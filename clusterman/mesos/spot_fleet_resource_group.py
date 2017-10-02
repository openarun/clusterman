import json
from collections import defaultdict

from cachetools.func import ttl_cache

from clusterman.aws.client import ec2
from clusterman.aws.client import ec2_describe_instances
from clusterman.aws.markets import get_instance_market
from clusterman.exceptions import ResourceGroupError
from clusterman.mesos.mesos_role_manager import MESOS_CACHE_TTL
from clusterman.mesos.mesos_role_resource_group import MesosRoleResourceGroup
from clusterman.mesos.mesos_role_resource_group import protect_unowned_instances
from clusterman.util import get_clusterman_logger

logger = get_clusterman_logger(__name__)


class SpotFleetResourceGroup(MesosRoleResourceGroup):

    def __init__(self, sfr_id):
        self.sfr_id = sfr_id
        self.market_weights = {
            get_instance_market(spec): spec['WeightedCapacity']
            for spec in self._configuration['SpotFleetRequestConfig']['LaunchSpecifications']
        }

    def market_weight(self, market):
        return self.market_weights[market]

    def modify_target_capacity(self, new_capacity, should_terminate=False):
        """Increase or decrease the (weighted) spot fleet target capacity

        :param new_capacity: the new desired capacity of the fleet, in resource units
        :param should_terminate: set to True to terminate instances when scaling down

        """
        termination_policy = 'Default' if should_terminate else 'NoTermination'
        response = ec2.modify_spot_fleet_request(
            SpotFleetRequestId=self.sfr_id,
            TargetCapacity=int(new_capacity),
            ExcessCapacityTerminationPolicy=termination_policy,
        )
        if not response['Return']:
            raise ResourceGroupError("Could not change size of spot fleet: {resp}".format(
                resp=json.dumps(response['ResponseMetadata']),
            ))

    @protect_unowned_instances
    def terminate_instances_by_id(self, instance_ids, batch_size=500):
        """Terminate instances in the spot fleet

        :param instance_ids_to_kill: a list of instances to terminate
        """
        if not instance_ids:
            logger.warn('No instances to terminate')
            return [], 0

        instance_weights = {
            instance['InstanceId']: self.market_weights[get_instance_market(instance)]
            for instance in ec2_describe_instances(instance_ids)
        }

        terminated_instance_ids = []
        for batch in range(0, len(instance_ids), batch_size):
            response = ec2.terminate_instances(InstanceIds=instance_ids[batch:batch + batch_size])
            terminated_instance_ids.extend([instance['InstanceId'] for instance in response['TerminatingInstances']])

        if sorted(terminated_instance_ids) != sorted(instance_ids):
            missing_instances = list(set(instance_ids) - set(terminated_instance_ids))
            logger.warn(f'Some instances were not terminated: {missing_instances}')

        terminated_weight = sum(instance_weights[i] for i in terminated_instance_ids)
        self.modify_target_capacity(self.target_capacity - terminated_weight)

        logger.info(f'Terminated weight: {terminated_weight}; instances: {terminated_instance_ids}')
        return terminated_instance_ids, terminated_weight

    @property
    def id(self):
        return self.sfr_id

    @property
    @ttl_cache(ttl=MESOS_CACHE_TTL)
    def instances(self):
        # TODO manually paginating until https://github.com/boto/botocore/pull/1286 is accepted into botocore
        next_token = ''
        instance_ids = []
        while True:
            instances = ec2.describe_spot_fleet_instances(SpotFleetRequestId=self.sfr_id, NextToken=next_token)
            instance_ids.extend([i['InstanceId'] for i in instances['ActiveInstances']])
            next_token = instances.get('NextToken', '')
            if not next_token:
                break
        return instance_ids

    @property
    def market_capacities(self):
        return {
            market: len(instances) * self.market_weights[market]
            for market, instances in self._instances_by_market.items()
        }

    @property
    def target_capacity(self):
        return self._configuration['SpotFleetRequestConfig']['TargetCapacity']

    @property
    def fulfilled_capacity(self):
        return self._configuration['SpotFleetRequestConfig']['FulfilledCapacity']

    @property
    def status(self):
        return self._configuration['SpotFleetRequestState']

    @property
    def _configuration(self):
        fleet_configuration = ec2.describe_spot_fleet_requests(SpotFleetRequestIds=[self.sfr_id])
        return fleet_configuration['SpotFleetRequestConfigs'][0]

    @ttl_cache(ttl=MESOS_CACHE_TTL)
    def _instances_by_market(self):
        instance_dict = defaultdict(list)
        for instance in ec2_describe_instances(self.instances):
            instance_dict[get_instance_market(instance)].append(instance)
        return instance_dict
