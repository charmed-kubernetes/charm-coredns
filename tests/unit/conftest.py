import unittest.mock as mock

import pytest
from ops.pebble import ServiceStatus
from ops.testing import Harness

from charm import CoreDNSCharm


# Autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def lk_client():
    with mock.patch("ops.manifests.manifest.Client", autospec=True) as mock_lightkube:
        yield mock_lightkube.return_value


@pytest.fixture()
def harness():
    harness = Harness(CoreDNSCharm)
    try:
        harness.disable_hooks()
        harness.set_model_name("test-model")
        yield harness
    finally:
        harness.cleanup()


@pytest.fixture()
def leader_harness(harness):
    harness.set_leader(True)
    return harness


@pytest.fixture()
def relation_harness(harness):
    relation_id = harness.add_relation("dns-provider", "kubernetes-control-plane")
    # Update the coredns side of the relation so ingress-address is present in the data
    harness.update_relation_data(
        relation_id, "coredns/0", {"ingress-address": "127.0.0.1"}
    )
    return relation_id
