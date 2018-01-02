from datetime import datetime

import mock
import pytest

from clusterman.exceptions import MesosRoleManagerError
from clusterman.mesos.util import allocated_cpu_resources
from clusterman.mesos.util import find_largest_capacity_market
from clusterman.mesos.util import get_mesos_state
from clusterman.mesos.util import mesos_post
from clusterman.mesos.util import MesosAgentState


@pytest.fixture
def mock_market_capacities():
    return {'market-1': 1000, 'market-2': 5}


@pytest.fixture
def mock_socket():
    with mock.patch('clusterman.mesos.util.socket') as mock_socket:
        mock_socket.gethostbyname.return_value = '1.2.3.4'
        yield


@mock.patch('clusterman.mesos.util.allocated_cpu_resources')
class TestGetMesosState:
    def test_orphaned(self, mock_allocated, mock_socket):
        instance = {'PrivateIpAddress': '1.2.3.4', 'LaunchTime': datetime.now()}
        agents = []
        mock_allocated.return_value = 100
        assert get_mesos_state(instance, agents) == MesosAgentState.ORPHANED

    def test_idle(self, mock_allocated, mock_socket):
        instance = {'PrivateIpAddress': '1.2.3.4', 'LaunchTime': datetime.now()}
        agents = [{'hostname': 'foo.com'}]
        mock_allocated.return_value = 0
        assert get_mesos_state(instance, agents) == MesosAgentState.IDLE

    def test_running(self, mock_allocated, mock_socket):
        instance = {'PrivateIpAddress': '1.2.3.4', 'LaunchTime': datetime.now()}
        agents = [{'hostname': 'foo.com'}]
        mock_allocated.return_value = 100
        assert get_mesos_state(instance, agents) == MesosAgentState.RUNNING

    def test_unknown(self, mock_allocated, mock_socket):
        instance = {}
        agents = [{'hostname': 'foo.com'}]
        mock_allocated.return_value = 100
        assert get_mesos_state(instance, agents) == MesosAgentState.UNKNOWN


def test_allocated_cpu_resources(mock_agents_response):
    assert allocated_cpu_resources(mock_agents_response.json()['slaves'][0]) == 0
    assert allocated_cpu_resources(mock_agents_response.json()['slaves'][1]) == 0
    assert allocated_cpu_resources(mock_agents_response.json()['slaves'][2]) == 10


def test_find_largest_capacity_market_no_threshold(mock_market_capacities):
    assert find_largest_capacity_market(mock_market_capacities) == ('market-1', 1000)


def test_find_largest_capacity_empty_list(mock_market_capacities):
    assert find_largest_capacity_market({}) == (None, 0)


@mock.patch('clusterman.mesos.util.mesos_post', wraps=mesos_post)
class TestMesosPost:
    def test_success(self, wrapped_post):
        with mock.patch('clusterman.mesos.util.requests'):
            wrapped_post('http://the.mesos.master/', 'an-endpoint')
        assert wrapped_post.call_count == 2
        assert wrapped_post.call_args_list == [
            mock.call('http://the.mesos.master/', 'an-endpoint'),
            mock.call('http://the.mesos.master/', 'redirect'),
        ]

    def test_failure(self, wrapped_post):
        with mock.patch('clusterman.mesos.util.requests') as mock_requests, \
                pytest.raises(MesosRoleManagerError):
            mock_requests.post.side_effect = Exception('something bad happened')
            wrapped_post('http://the.mesos.master/', 'an-endpoint')
