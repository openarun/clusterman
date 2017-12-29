import mock
import pytest
from moto import mock_ec2

from clusterman.aws.client import ec2
from clusterman.exceptions import MesosRoleManagerError
from clusterman.mesos.mesos_role_manager import MesosRoleManager
from clusterman.mesos.util import find_largest_capacity_market
from clusterman.mesos.util import MesosAgentState


@pytest.fixture
def mock_resource_groups():
    return [
        mock.Mock(
            id=f'sfr-{i}',
            instance_ids=[f'i-{i}'],
            target_capacity=i * 2 + 1,
            fulfilled_capacity=i * 6,
            market_capacities={'market-1': i, 'market-2': i * 2, 'market-3': i * 3},
        )
        for i in range(7)
    ]


@pytest.fixture
def mock_role_manager(mock_resource_groups):
    with mock.patch('clusterman.mesos.mesos_role_manager.load_spot_fleets_from_s3') as mock_load:
        mock_load.return_value = []
        manager = MesosRoleManager('mesos-test', 'bar')
        manager.resource_groups = mock_resource_groups

        return manager


def test_mesos_role_manager_init(mock_role_manager):
    assert mock_role_manager.role == 'bar'
    assert mock_role_manager.api_endpoint == 'http://the.mesos.leader:5050/'


def test_modify_target_capacity_no_resource_groups(mock_role_manager):
    mock_role_manager.resource_groups = []
    with pytest.raises(MesosRoleManagerError):
        mock_role_manager.modify_target_capacity(1234)


@pytest.mark.parametrize('constrain_return', [100, 50])
def test_modify_target_capacity(constrain_return, mock_role_manager):
    mock_role_manager.prune_excess_fulfilled_capacity = mock.Mock()

    mock_role_manager._constrain_target_capacity = mock.Mock(return_value=constrain_return)
    mock_role_manager._compute_new_resource_group_targets = mock.Mock(return_value=[0, 1, 2, 3, 4, 5, 6])
    assert mock_role_manager.modify_target_capacity(100) == constrain_return
    assert mock_role_manager._constrain_target_capacity.call_count == 1
    assert mock_role_manager.prune_excess_fulfilled_capacity.call_count == 0
    assert mock_role_manager._compute_new_resource_group_targets.call_count == 1
    for i, group in enumerate(mock_role_manager.resource_groups):
        assert group.modify_target_capacity.call_count == 1
        assert group.modify_target_capacity.call_args[0][0] == i


@mock.patch('clusterman.mesos.mesos_role_manager.logger')
@mock.patch('clusterman.mesos.mesos_role_manager.find_largest_capacity_market', wraps=find_largest_capacity_market)
@mock.patch('clusterman.mesos.mesos_role_manager.MesosRoleManager._find_resource_group')
@mock.patch('clusterman.mesos.mesos_role_manager.MesosRoleManager._idle_agents_by_market')
@mock.patch('clusterman.mesos.mesos_role_manager.MesosRoleManager.target_capacity', mock.PropertyMock(return_value=50))
@mock.patch(
    'clusterman.mesos.mesos_role_manager.MesosRoleManager.fulfilled_capacity',
    mock.PropertyMock(return_value=100),
)
class TestPruneFulfilledCapacity:
    def test_no_idle_instances(self, mock_idle_agents_by_market, mock_find_res_group,
                               mock_find_largest_capacity_market, mock_logger, mock_role_manager):
        mock_idle_agents_by_market.return_value = {}
        assert not mock_role_manager.prune_excess_fulfilled_capacity()

    def test_instance_error(self, mock_idle_agents_by_market, mock_find_res_group,
                            mock_find_largest_capacity_market, mock_logger, mock_role_manager):
        mock_idle_agents_by_market.return_value = {'market-1': ['agent-1']}
        mock_find_res_group.return_value = -1, None
        assert not mock_role_manager.prune_excess_fulfilled_capacity()
        assert mock_find_largest_capacity_market.call_count == 3
        assert mock_logger.warn.call_count == 1

    def test_protected_market(self, mock_idle_agents_by_market, mock_find_res_group,
                              mock_find_largest_capacity_market, mock_logger, mock_role_manager):
        mock_idle_agents_by_market.return_value = {'market-1': []}
        assert not mock_role_manager.prune_excess_fulfilled_capacity()
        assert mock_find_largest_capacity_market.call_count == 2

    def test_protected_group(self, mock_idle_agents_by_market, mock_find_res_group,
                             mock_find_largest_capacity_market, mock_logger, mock_role_manager):
        mock_idle_agents_by_market.return_value = {'market-1': ['agent-1']}
        index = 6
        res_group = mock_role_manager.resource_groups[index]
        res_group.market_weight.return_value = 10000
        mock_find_res_group.return_value = index, res_group
        assert not mock_role_manager.prune_excess_fulfilled_capacity()
        assert mock_find_largest_capacity_market.call_count == 3

    def test_can_prune(self, mock_idle_agents_by_market, mock_find_res_group,
                       mock_find_largest_capacity_market, mock_logger, mock_role_manager):
        mock_idle_agents_by_market.return_value = {'market-1': ['agent-1', 'agent-2'], 'market-2': ['agent-3']}
        index = 6
        res_group = mock_role_manager.resource_groups[index]
        res_group.market_weight.return_value = 1
        res_group.terminate_instances_by_id.side_effect = lambda x: x
        mock_find_res_group.return_value = index, res_group
        assert set(mock_role_manager.prune_excess_fulfilled_capacity()) == {'agent-1', 'agent-2', 'agent-3'}
        assert mock_find_largest_capacity_market.call_count == 6


def test_compute_new_resource_group_targets_no_unfilled_capacity(mock_role_manager):
    assert mock_role_manager._compute_new_resource_group_targets(mock_role_manager.target_capacity) == [
        group.target_capacity
        for group in (mock_role_manager.resource_groups)
    ]


@pytest.mark.parametrize('orig_targets', [10, 17])
def test_compute_new_resource_group_targets_all_equal(orig_targets, mock_role_manager):
    for group in mock_role_manager.resource_groups:
        group.target_capacity = orig_targets

    num_groups = len(mock_role_manager.resource_groups)
    new_targets = mock_role_manager._compute_new_resource_group_targets(105)
    assert sorted(new_targets) == [15] * num_groups


@pytest.mark.parametrize('orig_targets', [10, 17])
def test_compute_new_resource_group_targets_all_equal_with_remainder(orig_targets, mock_role_manager):
    for group in mock_role_manager.resource_groups:
        group.target_capacity = orig_targets

    new_targets = mock_role_manager._compute_new_resource_group_targets(107)
    assert sorted(new_targets) == [15, 15, 15, 15, 15, 16, 16]


def test_compute_new_resource_group_targets_uneven_scale_up(mock_role_manager):
    new_targets = mock_role_manager._compute_new_resource_group_targets(304)
    assert sorted(new_targets) == [43, 43, 43, 43, 44, 44, 44]


def test_compute_new_resource_group_targets_uneven_scale_down(mock_role_manager):
    for group in mock_role_manager.resource_groups:
        group.target_capacity += 20

    new_targets = mock_role_manager._compute_new_resource_group_targets(10)
    assert sorted(new_targets) == [1, 1, 1, 1, 2, 2, 2]


def test_compute_new_resource_group_targets_above_delta_scale_up(mock_role_manager):
    new_targets = mock_role_manager._compute_new_resource_group_targets(62)
    assert sorted(new_targets) == [7, 7, 7, 8, 9, 11, 13]


def test_compute_new_resource_group_targets_below_delta_scale_down(mock_role_manager):
    new_targets = mock_role_manager._compute_new_resource_group_targets(30)
    assert sorted(new_targets) == [1, 3, 5, 5, 5, 5, 6]


def test_compute_new_resource_group_targets_above_delta_equal_scale_up(mock_role_manager):
    for group in mock_role_manager.resource_groups[3:]:
        group.target_capacity = 20

    new_targets = mock_role_manager._compute_new_resource_group_targets(100)
    assert sorted(new_targets) == [6, 7, 7, 20, 20, 20, 20]


def test_compute_new_resource_group_targets_below_delta_equal_scale_down(mock_role_manager):
    for group in mock_role_manager.resource_groups[:3]:
        group.target_capacity = 1

    new_targets = mock_role_manager._compute_new_resource_group_targets(20)
    assert sorted(new_targets) == [1, 1, 1, 4, 4, 4, 5]


def test_compute_new_resource_group_targets_above_delta_equal_scale_up_2(mock_role_manager):
    for group in mock_role_manager.resource_groups[3:]:
        group.target_capacity = 20

    new_targets = mock_role_manager._compute_new_resource_group_targets(145)
    assert sorted(new_targets) == [20, 20, 21, 21, 21, 21, 21]


def test_compute_new_resource_group_targets_below_delta_equal_scale_down_2(mock_role_manager):
    for group in mock_role_manager.resource_groups[:3]:
        group.target_capacity = 1

    new_targets = mock_role_manager._compute_new_resource_group_targets(9)
    assert sorted(new_targets) == [1, 1, 1, 1, 1, 2, 2]


def test_constrain_target_capacity_below_min_per_group(mock_role_manager):
    with mock.patch('clusterman.mesos.mesos_role_manager.logger') as mock_logger:
        assert mock_role_manager._constrain_target_capacity(1000) == 345
        assert mock_role_manager._constrain_target_capacity(1) == 7
        assert mock_role_manager._constrain_target_capacity(42) == 42
        assert mock_logger.warn.call_count == 2


def test_constrain_target_capacity_below_overall_min(mock_role_manager):
    mock_role_manager.min_capacity = 10
    with mock.patch('clusterman.mesos.mesos_role_manager.logger') as mock_logger:
        assert mock_role_manager._constrain_target_capacity(1000) == 345
        assert mock_role_manager._constrain_target_capacity(1) == 10
        assert mock_role_manager._constrain_target_capacity(42) == 42
        assert mock_logger.warn.call_count == 2


def test_find_resource_group(mock_role_manager):
    index, group = mock_role_manager._find_resource_group('i-3')
    assert group.id == 'sfr-3'
    index, group = mock_role_manager._find_resource_group('i-9')
    assert group is None


def test_get_market_capacities(mock_role_manager):
    assert mock_role_manager._get_market_capacities() == {
        'market-1': sum(i for i in range(7)),
        'market-2': sum(i * 2 for i in range(7)),
        'market-3': sum(i * 3 for i in range(7)),
    }
    assert mock_role_manager._get_market_capacities(market_filter='market-2') == {
        'market-2': sum(i * 2 for i in range(7)),
    }


def test_target_capacity(mock_role_manager):
    assert mock_role_manager.target_capacity == sum(2 * i + 1 for i in range(7))


def test_fulfilled_capacity(mock_role_manager):
    assert mock_role_manager.fulfilled_capacity == sum(i * 6 for i in range(7))


@mock_ec2
def test_idle_agents_by_market(mock_role_manager):
    reservations = ec2.run_instances(ImageId='ami-foobar', MinCount=3, MaxCount=3, InstanceType='t2.nano')
    agents_list = [
        {'hostname': instance['PrivateIpAddress']}
        for instance in reservations['Instances']
    ]
    mock_agents = mock.PropertyMock(return_value=agents_list)
    mock_role_manager.resource_groups = [
        mock.Mock(instance_ids=[i['InstanceId'] for i in reservations['Instances']])
    ]

    with mock.patch('clusterman.mesos.mesos_role_manager.MesosRoleManager.agents', mock_agents), \
            mock.patch('clusterman.mesos.mesos_role_manager.get_mesos_state') as mock_mesos_state:
        mock_mesos_state.side_effect = [
            MesosAgentState.IDLE,
            MesosAgentState.ORPHANED,
            MesosAgentState.RUNNING,
        ]
        idle_agents_by_market = mock_role_manager._idle_agents_by_market()
        assert(len(list(idle_agents_by_market.values())[0]) == 2)


@mock.patch('clusterman.mesos.mesos_role_manager.mesos_post')
class TestAgentListing:
    def test_agent_list_error(self, mock_post, mock_role_manager):
        mock_post.side_effect = MesosRoleManagerError('dummy error')
        with pytest.raises(MesosRoleManagerError):
            mock_role_manager.agents

    def test_filter_roles(self, mock_post, mock_agents_response, mock_role_manager):
        mock_post.return_value = mock_agents_response
        agents = mock_role_manager.agents
        assert len(agents) == 1
        assert agents[0]['hostname'] == 'im-in-the-role.yelpcorp.com'

        # Multiple calls should have the same result.
        assert agents == mock_role_manager.agents
        assert mock_post.call_count == 2  # cache expires immediately in tests


class TestResources:
    @pytest.fixture
    def mock_agents(self, mock_role_manager):
        with mock.patch(
            'clusterman.mesos.mesos_role_manager.MesosRoleManager.agents',
            new_callable=mock.PropertyMock
        ) as mock_agents:
            mock_agents.return_value = [
                {
                    'id': 'idle',
                    'resources': {'cpus': 4, 'gpus': 2},
                },
                {
                    'id': 'no-gpus',
                    'resources': {'cpus': 8},
                    'used_resources': {'cpus': 1.5},
                },
                {
                    'id': 'gpus-1',
                    'resources': {'gpus': 2},
                    'used_resources': {'gpus': 1},
                },
                {
                    'id': 'gpus-2',
                    'resources': {'gpus': 4},
                    'used_resources': {'gpus': 0.2},
                },
            ]
            yield mock_role_manager

    @pytest.mark.parametrize('resource_name,expected', [
        ('cpus', 1.5),
        ('gpus', 1.2),
    ])
    def test_allocation(self, mock_agents, resource_name, expected):
        assert mock_agents.get_resource_allocation(resource_name) == expected

    @pytest.mark.parametrize('resource_name,expected', [
        ('cpus', 12),
        ('gpus', 8),
    ])
    def test_total_cpus(self, mock_agents, resource_name, expected):
        assert mock_agents.get_resource_total(resource_name) == expected

    @pytest.mark.parametrize('resource_name,expected', [
        ('mem', 0),
        ('cpus', 0.125),
        ('gpus', 0.15),
    ])
    def test_average_allocation(self, mock_agents, resource_name, expected):
        assert mock_agents.get_percent_resource_allocation(resource_name) == expected
