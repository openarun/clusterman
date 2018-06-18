import os
import re
from collections import defaultdict

import requests
import staticconf
from staticconf.config import DEFAULT as DEFAULT_NAMESPACE

from clusterman.config import get_cluster_config_directory
from clusterman.exceptions import MesosPoolManagerError
from clusterman.util import get_clusterman_logger

logger = get_clusterman_logger(__name__)


class MesosAgentState:
    IDLE = 'idle'
    ORPHANED = 'orphaned'
    RUNNING = 'running'
    UNKNOWN = 'unknown'


def agent_pid_to_ip(slave_pid):
    """Convert the agent PID from Mesos into an IP address

    :param: agent pid (this is in the format 'slave(1)@10.40.31.172:5051')
    :returns: ip address
    """
    regex = re.compile(r'.+?@([\d\.]+):\d+')
    return regex.match(slave_pid).group(1)


def _get_agent_by_ip(ip, mesos_agents):
    try:
        return next(agent for agent in mesos_agents if agent_pid_to_ip(agent['pid']) == ip)
    except StopIteration:
        return None


# TODO(CLUSTERMAN-256): refactor this into a more general method that handles
# creating unified representations of instances/agents.
def get_mesos_agent_and_state_from_aws_instance(instance, mesos_agents):
    try:
        instance_ip = instance['PrivateIpAddress']
    except KeyError:
        return None, MesosAgentState.UNKNOWN
    else:
        agent = _get_agent_by_ip(instance_ip, mesos_agents)
        if not agent:
            state = MesosAgentState.ORPHANED
        elif allocated_cpu_resources(agent) == 0:
            state = MesosAgentState.IDLE
        else:
            state = MesosAgentState.RUNNING

        return agent, state


def get_task_count_per_agent(mesos_tasks):
    """Given a list of mesos tasks, return a count of tasks per agent"""
    agent_id_to_task_count = defaultdict(int)
    for task in mesos_tasks:
        if task['state'] == 'TASK_RUNNING':
            agent_id_to_task_count[task['slave_id']] += 1
    return agent_id_to_task_count


def get_resource_value(resources, resource_name):
    """Helper to get the value of the given resource, from a list of resources returned by Mesos."""
    return resources.get(resource_name, 0)


def get_total_resource_value(agents, value_name, resource_name):
    """
    Get the total value of a resource type from the list of agents.

    :param agents: list of agents from Mesos
    :param value_name: desired resource value (e.g. total_resources, allocated_resources)
    :param resource_name: name of resource recognized by Mesos (e.g. cpus, memory, disk)
    """
    return sum(
        get_resource_value(agent.get(value_name, {}), resource_name)
        for agent in agents
    )


def allocated_cpu_resources(agent):
    return get_resource_value(agent.get('used_resources', {}), 'cpus')


def mesos_post(url, endpoint):
    master_url = url if endpoint == 'redirect' else mesos_post(url, 'redirect').url + '/'
    request_url = master_url + endpoint
    response = None
    try:
        response = requests.post(
            request_url,
            headers={'user-agent': 'clusterman'},
        )
        response.raise_for_status()
    except Exception as e:  # there's no one exception class to check for problems with the request :(
        log_message = (
            f'Mesos is unreachable:\n\n'
            f'{str(e)}\n'
            f'Querying Mesos URL: {request_url}\n'
        )
        if response is not None:
            log_message += (
                f'Response Code: {response.status_code}\n'
                f'Response Text: {response.text}\n'
            )
        logger.critical(log_message)
        raise MesosPoolManagerError(f'Mesos master unreachable: check the logs for details') from e

    return response


def get_cluster_name_list(config_namespace=DEFAULT_NAMESPACE):
    namespace = staticconf.config.get_namespace(config_namespace)
    return namespace.get_config_dict().get('mesos_clusters', {}).keys()


def get_pool_name_list(cluster_name):
    cluster_config_directory = get_cluster_config_directory(cluster_name)
    return [
        f[:-5] for f in os.listdir(cluster_config_directory)
        if f[0] != '.' and f[-5:] == '.yaml'  # skip dotfiles and only read yaml-files
    ]
