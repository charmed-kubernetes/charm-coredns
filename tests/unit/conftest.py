from unittest.mock import patch

import pytest
from charm import CoreDNSCharm
from ops.pebble import ServiceStatus
from ops.testing import Harness


# Autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def mocked_service_patch(mocker):
    mocked_service_patch = mocker.patch("charm.KubernetesServicePatch")
    yield mocked_service_patch


# Autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def mocked_lightkube_client(mocker):
    with patch("charm.Client") as mock_client:
        yield mock_client


@pytest.fixture()
def harness(mocker):
    harness = Harness(CoreDNSCharm)
    harness.set_model_name("coredns-model")
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    harness.container_pebble_ready("coredns")
    harness.model.get_binding = mocker.MagicMock()
    return harness


@pytest.fixture()
def container(harness, mocker):
    container = harness.model.unit.get_container("coredns")
    container.push = mocker.MagicMock()
    container.stop = mocker.MagicMock()
    container.start = mocker.MagicMock()
    return container


@pytest.fixture()
def active_service(mocker):
    mocked_service = mocker.MagicMock()
    mocked_service.current = ServiceStatus.ACTIVE
    return mocked_service


@pytest.fixture()
def inactive_service(mocker):
    mocked_service = mocker.MagicMock()
    mocked_service.current = ServiceStatus.INACTIVE
    return mocked_service


@pytest.fixture()
def active_container(mocker, container, active_service):
    container.get_service = mocker.MagicMock(return_value=active_service)
    return container


@pytest.fixture()
def inactive_container(mocker, container, inactive_service):
    container.get_service = mocker.MagicMock(return_value=inactive_service)
    return container


@pytest.fixture()
def relation_harness(mocker):
    harness = Harness(CoreDNSCharm)
    harness.set_can_connect("coredns", True)
    container = harness.model.unit.get_container("coredns")
    container.stop = mocker.MagicMock()
    container.start = mocker.MagicMock()
    harness.set_leader(True)
    return harness


@pytest.fixture()
def relation_with_ingress(relation_harness):
    relation_id = relation_harness.add_relation("dns-provider", "kubernetes-master")
    # Update the coredns side of the relation so ingress-address is present in the data
    relation_harness.update_relation_data(
        relation_id, "coredns/0", {"ingress-address": "127.0.0.1"}
    )
    return relation_id
