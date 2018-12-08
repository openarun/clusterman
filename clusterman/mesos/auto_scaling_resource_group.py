import pprint
from collections import defaultdict
from typing import Any
from typing import Dict
from typing import Mapping
from typing import Sequence

import colorlog
import simplejson as json
from cached_property import timed_cached_property
from mypy_extensions import TypedDict

from clusterman.aws.client import autoscaling
from clusterman.aws.client import ec2
from clusterman.aws.markets import EC2_INSTANCE_TYPES
from clusterman.aws.markets import InstanceMarket
from clusterman.mesos.constants import CACHE_TTL_SECONDS
from clusterman.mesos.mesos_pool_resource_group import MesosPoolResourceGroup
from clusterman.mesos.mesos_pool_resource_group import protect_unowned_instances

_BATCH_TERM_SIZE = 200

logger = colorlog.getLogger(__name__)


AutoScalingResourceGroupConfig = TypedDict(
    'AutoScalingResourceGroupConfig',
    {
        'tag': str,
    }
)


class AutoScalingResourceGroup(MesosPoolResourceGroup):
    """
    Auto Scaling Groups (ASGs)
    """

    def __init__(self, group_id: str) -> None:
        self.group_id = group_id  # ASG id

        self._protect_instances(protect=True)

    @timed_cached_property(ttl=CACHE_TTL_SECONDS)
    def _group_config(self) -> Dict[str, Any]:
        """ Retrieve our ASG's configuration from AWS.

        Note: Response from this API call are cached to prevent hitting any AWS
        request limits.
        """
        response = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[self.group_id],
        )
        return response['AutoScalingGroups'][0]

    @timed_cached_property(ttl=CACHE_TTL_SECONDS)
    def _launch_config(self) -> Dict[str, Any]:
        """ Retrieve our ASG's launch configuration from AWS

        Note: Response from this API call are cached to prevent hitting any AWS
        request limits.
        """
        response = autoscaling.describe_launch_configurations(
            LaunchConfigurationNames=[
                self._group_config['LaunchConfigurationName'],
            ],
        )
        return response['LaunchConfigurations'][0]

    def _protect_instances(self, *, protect: bool = True) -> None:
        """ Toggle scale-in protection for new and current instances

        Note: Does not prevent instances from being terminated via the EC2
        client.
        """
        autoscaling.update_auto_scaling_group(  # protect new
            AutoScalingGroupName=self.id,
            NewInstancesProtectedFromScaleIn=protect,
        )
        if self.instance_ids:
            autoscaling.set_instance_protection(  # protect current
                InstanceIds=self.instance_ids,
                AutoScalingGroupName=self.id,
                ProtectedFromScaleIn=protect,
            )

    def market_weight(self, market: InstanceMarket) -> float:
        """ Returns the weight of a given market

        ASGs have no concept of weight, so if ASG is available in the market's
        AZ and matches the ASG's instance type, we return the nubmer of CPUs
        for that market.

        :param market: The market for which we want the weight for
        :returns: The weight of a given market
        """
        if (market.az in self._group_config['AvailabilityZones'] and
                market.instance == self._launch_config['InstanceType']):
            return EC2_INSTANCE_TYPES[market.instance].cpus
        else:
            return 0

    def modify_target_capacity(
        self,
        target_capacity: float,
        *,
        terminate_excess_capacity: bool = False,
        dry_run: bool = False,
        honor_cooldown: bool = False,
    ) -> None:
        """ Modify the desired capacity for the ASG.

        :param target_capacity: The new desired number of instances in th ASG.
            Must be such that the desired capacity is between the minimum and
            maximum capacities of the ASGs. The desired capacity will be rounded
            to the minimum or maximum otherwise, whichever is closer.
        :param terminate_excess_capacity: Boolean indicating whether or not to
            terminate excess instances in the event of a scale down
        :param dry_run: Boolean indicating whether or not to take action or just
            log
        :param honor_cooldown: Boolean for whether or not to wait for a period
            of time (cooldown, set in ASG config) after the previous scaling
            activity has completed before initiating this one. Defaults to False,
            which is the AWS default for manual scaling activities.
        """
        # Round target_cpacity to min or max if necessary
        max_size = self._group_config['MaxSize']
        min_size = self._group_config['MinSize']
        if target_capacity > max_size:
            logger.warn(
                f'New target_capacity={target_capacity} exceeds ASG MaxSize={max_size}, '
                'setting to max instead'
            )
            target_capacity = max_size
        elif target_capacity < min_size:
            logger.warn(
                f'New target_capacity={target_capacity} falls below ASG MinSize={min_size}, '
                'setting to min instead'
            )
            target_capacity = min_size

        kwargs = dict(
            AutoScalingGroupName=self.group_id,
            DesiredCapacity=int(target_capacity),
            HonorCooldown=honor_cooldown,
        )
        logger.info(
            'Setting target capacity for ASG with arguments:\n'
            f'{pprint.pformat(kwargs)}'
        )
        if dry_run:
            return

        target_diff = self.target_capacity - target_capacity
        if target_diff > 0 and terminate_excess_capacity:
            # By default, we protect instances from scale down, so we need to
            # remove that protection on some if we want to terminate
            autoscaling.set_instance_protection(
                InstanceIds=self.instance_ids[:int(target_diff)],
                AutoScalingGroupName=self.id,
                ProtectedFromScaleIn=False,
            )
        autoscaling.set_desired_capacity(**kwargs)

    @protect_unowned_instances
    def terminate_instances_by_id(
        self,
        instance_ids: Sequence[str],
    ) -> Sequence[str]:
        """ Terminate instances in the ASG

        The autoscaling client does not support batch termination, only
        instance-by-instance termination. To avoid hitting AWS request limits,
        we use the EC2 client to batch terminate instances.

        :param instance_ids: A list of instance IDs to terminate
        :param decrement_desired_capacity: Boolean for whether or not to
            decrement desired capacity in the event of a scale down
        :returns: A list of terminated instance IDs
        """
        if not instance_ids:
            logger.warn(f'No instances to terminate in {self.group_id}')
            return []

        # terminate instances
        terminated_ids = []
        for i in range(0, len(instance_ids), _BATCH_TERM_SIZE):
            # Technically, we should only be using the autoscaling client to
            # terminate instances, since ASG instances typically have
            # termination hooks to complete. Additionally, we assume that
            # MesosPoolManager will manage things for us.
            response = ec2.terminate_instances(InstanceIds=instance_ids)
            terminated_ids.extend([
                inst['InstanceId']
                for inst in response['TerminatingInstances']
            ])

        # It's possible that not every instance appears terminated, probably
        # because AWS already terminated them. Log just in case.
        missing_ids = set(instance_ids) - set(terminated_ids)
        if missing_ids:
            logger.warn(
                'Some instances could not be terminated; '
                'they were probably killed previously'
            )
            logger.warn(f'Missing instances: {list(missing_ids)}')

        logger.info(f'ASG {self.id}: terminated: {terminated_ids}')
        return terminated_ids

    @property
    def id(self) -> str:
        """ Returns the ASG's id """
        return self.group_id

    @timed_cached_property(ttl=CACHE_TTL_SECONDS)
    def instance_ids(self) -> Sequence[str]:
        """ Returns a list of instance IDs belonging to this ASG.

        Note: Response from this API call are cached to prevent hitting any AWS
        request limits.
        """
        return [
            inst['InstanceId']
            for inst in self._group_config['Instances']
            if inst is not None
        ]

    @property
    def market_capacities(self) -> Mapping[InstanceMarket, float]:
        """ Returns the total capacity (number of instances) per market that the
        ASG is in.
        """
        instances_by_market: Dict[InstanceMarket, float] = defaultdict(float)
        for inst in self._group_config['Instances']:
            inst_market = InstanceMarket(
                self._launch_config['InstanceType'],  # type uniform in asg
                inst['AvailabilityZone'],
            )
            instances_by_market[inst_market] += 1
        return instances_by_market

    @property
    def target_capacity(self) -> float:
        return self._group_config['DesiredCapacity']

    @property
    def fulfilled_capacity(self) -> float:
        return len(self._group_config['Instances'])

    @property
    def status(self) -> str:
        """ The status of the ASG

        An ASG either exists or it doesn't. Thus, if we can query its status,
        it is active.
        """
        return 'active'

    @property
    def is_stale(self) -> bool:
        """ Whether or not the ASG is stale

        An ASG either exists or it doesn't. Thus, the concept of staleness
        doesn't exist.
        """
        return False

    @staticmethod
    def load(
        cluster: str,
        pool: str,
        config: AutoScalingResourceGroupConfig,
    ) -> Mapping[str, 'MesosPoolResourceGroup']:
        """
        Loads a list of ASGs in the given cluster and pool

        :param cluster: A cluster name
        :param pool: A pool name
        :param config: An ASG config
        :returns: A dictionary of autoscaling resource groups, indexed by the id
        """
        asg_tags = _get_asg_tags()
        asgs = {}
        for asg_id, tags in asg_tags.items():
            try:
                puppet_role_tags = json.loads(tags[config['tag']])
                if (puppet_role_tags['pool'] == pool and
                        puppet_role_tags['paasta_cluster'] == cluster):
                    asgs[asg_id] = AutoScalingResourceGroup(asg_id)
            except KeyError:
                continue
        return asgs


def _get_asg_tags() -> Mapping[str, Mapping[str, str]]:
    """ Retrieves the tags for each ASG """
    asg_id_to_tags = {}
    for page in autoscaling.get_paginator('describe_auto_scaling_groups').paginate():
        for asg in page['AutoScalingGroups']:
            tags_dict = {tag['Key']: tag['Value'] for tag in asg['Tags']}
            asg_id_to_tags[asg['AutoScalingGroupName']] = tags_dict
    return asg_id_to_tags
