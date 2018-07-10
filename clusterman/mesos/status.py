import sys
from typing import Optional

import arrow
import humanize

from clusterman.args import add_cluster_arg
from clusterman.args import add_pool_arg
from clusterman.args import subparser
from clusterman.mesos.mesos_pool_manager import MesosPoolManager
from clusterman.mesos.mesos_pool_manager import PoolInstance
from clusterman.mesos.util import allocated_cpu_resources
from clusterman.mesos.util import MesosAgentState
from clusterman.util import colored_status


def _write_resource_group_line(group):
    # TODO (CLUSTERMAN-100) These are just the status responses for spot fleets; this probably won't
    # extend to other types of resource groups, so we should figure out what to do about that.
    status_str = colored_status(
        group.status,
        green=('active',),
        blue=('modifying', 'submitted'),
        red=('cancelled', 'failed', 'cancelled_running', 'cancelled_terminating'),
    )
    print(f'\t{group.id}: {status_str} ({group.fulfilled_capacity} / {group.target_capacity})')


def _write_instance_line(instance: PoolInstance, postfix: Optional[str]=None):
    postfix = postfix or ''
    instance_status_str = colored_status(
        instance.instance_dict['State']['Name'],
        green=('running',),
        blue=('pending',),
        red=('shutting-down', 'terminated', 'stopping', 'stopped'),
    )
    try:
        instance_ip = instance.instance_dict['PrivateIpAddress']
    except KeyError:
        instance_ip = 'unknown'
    instance_weight = instance.resource_group.market_weight(instance.market)
    print(f'\t - {instance.instance_id} {instance.market} {instance_weight} ({instance_ip}): {instance_status_str} {postfix}')


def _write_summary(manager):
    print('Mesos statistics:')
    total_cpus = manager.get_resource_total('cpus')
    total_mem = humanize.naturalsize(manager.get_resource_total('mem') * 1000000)
    total_disk = humanize.naturalsize(manager.get_resource_total('disk') * 1000000)
    allocated_cpus = manager.get_resource_allocation('cpus')
    allocated_mem = humanize.naturalsize(manager.get_resource_allocation('mem') * 1000000)
    allocated_disk = humanize.naturalsize(manager.get_resource_allocation('disk') * 1000000)
    print(f'\tCPU allocation: {allocated_cpus} CPUs allocated to tasks, {total_cpus} total')
    print(f'\tMemory allocation: {allocated_mem} memory allocated to tasks, {total_mem} total')
    print(f'\tDisk allocation: {allocated_disk} disk space allocated to tasks, {total_disk} total')


def _get_mesos_status_string(instance):
    if instance.state == MesosAgentState.UNKNOWN:
        postfix_str = ''
    elif instance.state == MesosAgentState.RUNNING:
        allocated_cpus = allocated_cpu_resources(instance.agent)
        postfix_str = f', {allocated_cpus} CPUs allocated, {instance.task_count} tasks'
    else:
        launch_time = instance.instance_dict['LaunchTime']
        uptime = humanize.naturaldelta(arrow.now() - arrow.get(launch_time))
        postfix_str = f', up for {uptime}'

    return colored_status(
        instance.state,
        blue=(MesosAgentState.IDLE,),
        red=(MesosAgentState.ORPHANED, MesosAgentState.UNKNOWN),
        prefix='[',
        postfix=postfix_str + ']',
    )


def print_status(manager, args):
    sys.stdout.write('\n')
    print(f'Current status for the {manager.pool} pool in the {manager.cluster} cluster:\n')
    print(f'Resource groups ({manager.fulfilled_capacity} units out of {manager.target_capacity}):')

    instances_by_group_id = manager.get_instances_by_resource_group() if args.verbose else {}

    for group in manager.resource_groups:
        _write_resource_group_line(group)
        for instance in instances_by_group_id.get(group.id, []):
            if ((args.only_orphans and instance.state != MesosAgentState.ORPHANED) or
                    (args.only_idle and instance.state != MesosAgentState.IDLE)):
                continue
            postfix = _get_mesos_status_string(instance)
            _write_instance_line(instance, postfix)

        sys.stdout.write('\n')

    _write_summary(manager)
    sys.stdout.write('\n')


def main(args):  # pragma: no cover
    manager = MesosPoolManager(args.cluster, args.pool)
    print_status(manager, args)


@subparser('status', 'check the status of a Mesos cluster', main)
def add_mesos_status_parser(subparser, required_named_args, optional_named_args):  # pragma: no cover
    add_cluster_arg(required_named_args, required=True)
    add_pool_arg(required_named_args, required=True)

    optional_named_args.add_argument(
        '--only-idle',
        action='store_true',
        help='Only show information about idle agents'
    )
    optional_named_args.add_argument(
        '--only-orphans',
        action='store_true',
        help='Only show information about orphaned instances (instances that are not in the Mesos cluster)'
    )
    optional_named_args.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Show more detailed status information',
    )
