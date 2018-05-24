import os
import re
import socket
import struct
import subprocess
import time
from collections import namedtuple
from threading import Thread

import simplejson as json
import staticconf
import yaml
from staticconf.errors import ConfigurationError

from clusterman.aws.client import s3
from clusterman.exceptions import NoSignalConfiguredException
from clusterman.exceptions import SignalConnectionError
from clusterman.exceptions import SignalValidationError
from clusterman.util import get_clusterman_logger

logger = get_clusterman_logger(__name__)
SignalConfig = namedtuple(
    'SignalConfig',
    ['name', 'branch_or_tag', 'period_minutes', 'required_metrics', 'parameters'],
)
MetricConfig = namedtuple('MetricConfig', ['name', 'type', 'minute_range'])
AutoscalingConfig = namedtuple(
    'AutoscalingConfig',
    ['setpoint', 'setpoint_margin', 'cpus_per_weight']
)
LOG_STREAM_NAME = 'tmp_clusterman_scaling_decisions'
SIGNALS_REPO = 'git@git.yelpcorp.com:clusterman_signals'
SOCKET_TIMEOUT_SECONDS = 60
SOCK_MESG_SIZE = 4096
ACK = bytes([1])


def _get_signal_loggers(signal_name):
    return get_clusterman_logger(f'{signal_name}.stdout'), get_clusterman_logger(f'{signal_name}.stderr')


def _log_subprocess_run(*args, **kwargs):
    result = subprocess.run(*args, **kwargs)
    logger.info(result.stdout.decode().strip())
    result.check_returncode()


def _log_signal_output(fd, log_fn):
    while True:
        line = fd.readline().decode().strip()
        if not line:
            break
        log_fn(line)


def _init_signal_output_pipes(signal_name, signal_process):
    stdout_logger, stderr_logger = _get_signal_loggers(signal_name)
    stdout_thread = Thread(
        target=_log_signal_output,
        kwargs={'fd': signal_process.stdout, 'log_fn': stdout_logger.info},
        daemon=True,
    )
    stderr_thread = Thread(
        target=_log_signal_output,
        kwargs={'fd': signal_process.stderr, 'log_fn': stderr_logger.warn},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()


def _get_cache_location():
    """ Store clusterman-specific cached data in ~/.cache/clusterman """
    return os.path.join(os.path.expanduser("~"), '.cache', 'clusterman')


def _sha_from_branch_or_tag(branch_or_tag):
    """ Convert a branch or tag for clusterman_signals into a git SHA """
    result = subprocess.run(
        ['git', 'ls-remote', '--exit-code', SIGNALS_REPO, branch_or_tag],
        stdout=subprocess.PIPE,
        check=True,
    )
    output = result.stdout.decode()
    sha = output.split('\t')[0]
    return sha


def _get_local_signal_directory(branch_or_tag):
    """ Get the directory path for the local version of clusterman_signals corresponding to a
    particular branch or tag.  Stores the signal in ~/.cache/clusterman/clusterman_signals_{git_sha}
    """
    local_repo_cache = _get_cache_location()
    sha = _sha_from_branch_or_tag(branch_or_tag)
    local_path = os.path.join(local_repo_cache, f'clusterman_signals_{sha}')
    subprocess_kwargs = {'cwd': local_path, 'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT}

    # If we don't have a local copy of the signal, clone it
    if not os.path.exists(local_path):
        # clone the clusterman_signals repo with a specific version into the path at local_path
        # --depth 1 says to squash all the commits to minimize data transfer/disk space
        os.makedirs(local_path)
        _log_subprocess_run(
            ['git', 'clone', '--depth', '1', '--branch', branch_or_tag, SIGNALS_REPO, local_path],
            **subprocess_kwargs,
        )
    else:
        logger.debug(f'signal version {sha} exists in cache, not re-cloning')

    # Alwasy re-build the signal's virtualenv
    _log_subprocess_run(['make', 'clean'], **subprocess_kwargs)
    _log_subprocess_run(['make', 'prod'], **subprocess_kwargs)

    return local_path


def get_autoscaling_config(config_namespace):
    """Load autoscaling configuration values from the provided config_namespace, falling back to the
    values stored in the default namespace if none are specified.

    :param config_namespace: namespace to read from before falling back to the default namespace
    :returns: AutoscalingConfig object with loaded config values
    """
    default_setpoint = staticconf.read_float('autoscaling.setpoint')
    default_setpoint_margin = staticconf.read_float('autoscaling.setpoint_margin')
    default_cpus_per_weight = staticconf.read_int('autoscaling.cpus_per_weight')

    reader = staticconf.NamespaceReaders(config_namespace)
    return AutoscalingConfig(
        setpoint=reader.read_float('autoscaling.setpoint', default=default_setpoint),
        setpoint_margin=reader.read_float('autoscaling.setpoint_margin', default=default_setpoint_margin),
        cpus_per_weight=reader.read_int('autoscaling.cpus_per_weight', default=default_cpus_per_weight),
    )


def read_signal_config(config_namespace, metrics_index):
    """Validate and return autoscaling signal config from the given namespace.

    :param config_namespace: namespace to read values from
    :returns: SignalConfig object with the values filled in
    :raises staticconf.errors.ConfigurationError: if the config namespace is missing a required value
    :raises NoSignalConfiguredException: if the config namespace doesn't define a custom signal
    :raises SignalValidationError: if some signal parameter is incorrectly set
    """
    reader = staticconf.NamespaceReaders(config_namespace)
    try:
        name = reader.read_string('autoscale_signal.name')
    except ConfigurationError:
        raise NoSignalConfiguredException(f'No signal was configured in {config_namespace}')

    period_minutes = reader.read_int('autoscale_signal.period_minutes')
    if period_minutes <= 0:
        raise SignalValidationError(f'Length of signal period must be positive, got {period_minutes}')
    metrics_dict_list = reader.read_list('autoscale_signal.required_metrics', default=[])
    if metrics_index is not None:
        metrics_dict_list = update_metrics_dict_list(metrics_dict_list, metrics_index)

    parameter_dict_list = reader.read_list('autoscale_signal.parameters', default=[])

    parameter_dict = {key: value for param_dict in parameter_dict_list for (key, value) in param_dict.items()}

    branch_or_tag = reader.read_string('autoscale_signal.branch_or_tag')
    required_metric_keys = set(MetricConfig._fields)
    metric_configs = []
    for metrics_dict in metrics_dict_list:
        missing = required_metric_keys - set(metrics_dict.keys())
        if missing:
            raise SignalValidationError(f'Missing required metric keys {missing} in {metrics_dict}')
        metric_config = {key: metrics_dict[key] for key in metrics_dict if key in required_metric_keys}
        metric_configs.append(MetricConfig(**metric_config))
    logger.info("Reading metrics config for these metrics = {}".format(metric_configs))
    return SignalConfig(name, branch_or_tag, period_minutes, metric_configs, parameter_dict)


def load_signal_connection(branch_or_tag, role, signal_name):
    """ Create a connection to the specified signal over a unix socket

    :param branch_or_tag: the git branch or tag for the version of the signal to use
    :param role: the role we are loading the signal for
    :param signal_name: the name of the signal we want to load
    :returns: a socket connection which can read/write data to the specified signal
    """
    signal_dir = _get_local_signal_directory(branch_or_tag)

    # this creates an abstract namespace socket which is auto-cleaned on program exit
    s = socket.socket(socket.AF_UNIX)
    s.bind(f'\0{role}-{signal_name}-socket')
    s.listen(1)  # only allow one connection at a time
    s.settimeout(SOCKET_TIMEOUT_SECONDS)

    # We have to *create* the socket before starting the subprocess so that the subprocess
    # will be able to connect to it, but we have to start the subprocess before trying to
    # accept connections, because accept blocks
    signal_process = subprocess.Popen(
        [
            os.path.join(signal_dir, 'prodenv', 'bin', 'python'),
            '-m',
            'clusterman_signals.run',
            role,
            signal_name,
        ],
        cwd=signal_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _init_signal_output_pipes(signal_name, signal_process)
    time.sleep(2)  # Give the signal subprocess time to start, then check to see if it's running
    return_code = signal_process.poll()
    if return_code:
        raise SignalConnectionError(f'Could not load signal {signal_name}; aborting')
    signal_conn, __ = s.accept()
    signal_conn.settimeout(SOCKET_TIMEOUT_SECONDS)
    return signal_conn


def evaluate_signal(metrics, timestamp, signal_conn):
    """ Communicate over a Unix socket with the signal to evaluate its result

    :param metrics: a dict of metric_name -> timeseries data to send to the signal
    :param timestamp: a Unix timestamp to pass to the signal as the "current time"
    :param signal_conn: an active Unix socket connection
    :returns: a dict of resource_name -> requested resources from the signal
    :raises SignalConnectionError: if the signal connection fails for some reason
    """
    # First send the length of the metrics data
    metric_bytes = json.dumps({'metrics': metrics, 'timestamp': timestamp.timestamp}).encode()
    len_metrics = struct.pack('>I', len(metric_bytes))  # bytes representation of the length, packed big-endian
    signal_conn.send(len_metrics)
    response = signal_conn.recv(SOCK_MESG_SIZE)
    if response != ACK:
        raise SignalConnectionError(f'Unknown error occurred sending metric length to signal (response={response})')

    # Then send the actual metrics data, broken up into chunks
    for i in range(0, len(metric_bytes), SOCK_MESG_SIZE):
        signal_conn.send(metric_bytes[i:i + SOCK_MESG_SIZE])
    response = signal_conn.recv(SOCK_MESG_SIZE)
    ack_bit = response[:1]
    if ack_bit != ACK:
        raise SignalConnectionError(f'Unknown error occurred sending metric data to signal (response={response})')

    # Sometimes the signal sends the ack and the reponse "too quickly" so when we call
    # recv above it gets both values.  This should handle that case, or call recv again
    # if there's no more data in the previous message
    response = response[1:] or signal_conn.recv(SOCK_MESG_SIZE)
    return json.loads(response)['Resources']


def get_metrics_index_from_s3(metrics_index_bucket, mesos_region):
    keyname = f'{mesos_region}.yaml'
    metrics_index_object = s3.get_object(Bucket=metrics_index_bucket, Key=keyname)
    return yaml.load(metrics_index_object['Body'])


def update_metrics_dict_list(metrics_dict_list, metrics_index):
    update_metrics_dict_list = list()
    for metric_dict in metrics_dict_list:
        metric_type = metric_dict['type']
        metric_regex = re.compile(metric_dict['name'])
        for metric_name in filter(metric_regex.match, metrics_index[metric_type]):
            update_metric_dict = dict(metric_dict)
            update_metric_dict['name'] = metric_name
            update_metrics_dict_list.append(update_metric_dict)

    return update_metrics_dict_list
