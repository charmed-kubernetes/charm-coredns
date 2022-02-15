from unittest.mock import call
import logging

from charm import CoreDNSCharm
from ops.testing import Harness


def test_not_leader():
    harness = Harness(CoreDNSCharm)
    harness.begin()
    assert harness.charm.model.unit.status.name == "waiting"


def test_coredns_pebble_ready(harness, container):
    initial_plan = harness.get_container_pebble_plan("coredns")
    assert initial_plan.to_yaml() == "{}\n"
    expected_plan = {
        "services": {
            "coredns": {
                "override": "replace",
                "summary": "CoreDNS",
                "command": "/coredns -conf /etc/coredns/Corefile",
                "startup": "enabled",
            }
        },
    }
    harness.charm.on.coredns_pebble_ready.emit(container)
    updated_plan = harness.get_container_pebble_plan("coredns").to_dict()
    assert expected_plan == updated_plan
    service = harness.model.unit.get_container("coredns").get_service("coredns")
    assert service.is_running()
    assert harness.model.unit.status.name == "active"


def test_coredns_pebble_ready_already_started(harness, active_container, caplog):
    with caplog.at_level(logging.INFO):
        harness.charm.on.coredns_pebble_ready.emit(active_container)
    assert "CoreDNS already started" in caplog.text


def test_config_changed(
    harness, active_container, caplog, corefile_base, extra_server, corefile_extra
):
    harness.update_config({"forward": "1.1.1.1"})
    harness.update_config({"extra_servers": extra_server})
    active_container.push.assert_has_calls(
        [
            call("/etc/coredns/Corefile", corefile_base, make_dirs=True),
            call("/etc/coredns/Corefile", corefile_extra, make_dirs=True),
        ]
    )


def test_config_changed_not_running(harness, inactive_container, caplog):
    with caplog.at_level(logging.INFO):
        harness.update_config({"forward": "1.1.1.1"})
    assert "CoreDNS is not running" in caplog.text


def test_dns_provider_relation_changed(harness, active_container):
    relation_id = harness.add_relation("dns-provider", "kubernetes-master")
    harness.add_relation_unit(relation_id, "kubernetes-master/0")
    # Update the coredns side of the relation so ingres-address is present in the data
    harness.update_relation_data(
        relation_id, "coredns/0", {"ingress-address": "127.0.0.1"}
    )
    # Update the other side of the relation to trigger a relation changed event
    harness.update_relation_data(relation_id, "kubernetes-master", {})
    assert harness.get_relation_data(relation_id, "coredns/0") == {
        "domain": "cluster.local",
        "sdn-ip": "127.0.0.1",
        "port": "53",
        "ingress-address": "127.0.0.1",
    }
    assert harness.model.unit.status.name == "active"


def test_dns_provider_relation_changed_no_ingress_address(harness, active_container):
    relation_id = harness.add_relation("dns-provider", "kubernetes-master")
    harness.add_relation_unit(relation_id, "kubernetes-master/0")
    # Update the other side of the relation to trigger a relation changed event
    # Note that the ingress address will not be present in the coredns-side's data
    harness.update_relation_data(relation_id, "kubernetes-master", {})
    assert harness.model.unit.status.name == "maintenance"


def test_dns_provider_relation_changed_not_running(harness, inactive_container):
    relation_id = harness.add_relation("dns-provider", "kubernetes-master")
    harness.add_relation_unit(relation_id, "kubernetes-master/0")
    harness.update_relation_data(
        relation_id, "coredns/0", {"ingress-address": "127.0.0.1"}
    )
    # Update the other side of the relation to trigger a relation changed event
    harness.update_relation_data(relation_id, "kubernetes-master", {})
    assert harness.model.unit.status.name == "waiting"
