from unittest.mock import call
import logging
from charm import CoreDNSCharm
from ops.testing import Harness
from ops.model import ActiveStatus
from string import Template

logger = logging.getLogger(__name__)


def test_not_leader():
    harness = Harness(CoreDNSCharm)
    harness.begin()
    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


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


def test_config_changed(harness, active_container, caplog):

    extra_servers = """. {
log
}
"""
    forward = "1.1.1.1"
    domain = "some.domain"
    corefile_template = Template(harness.model.config["corefile"])
    corefile_base = corefile_template.safe_substitute(
        {"domain": domain, "forward": forward, "extra_servers": ""}
    )
    corefile_extra = corefile_template.safe_substitute(
        {"domain": domain, "forward": forward, "extra_servers": extra_servers}
    )

    harness.update_config({"domain": domain, "forward": forward})
    harness.update_config({"extra_servers": extra_servers})

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


def test_dns_provider_relation_created(
    relation_harness, relation_with_ingress, active_service, mocker
):
    container = relation_harness.model.unit.get_container("coredns")
    container.get_service = mocker.MagicMock(return_value=active_service)
    relation_harness.begin_with_initial_hooks()
    assert relation_harness.get_relation_data(relation_with_ingress, "coredns/0") == {
        "ingress-address": "127.0.0.1",
        "domain": "cluster.local",
        "sdn-ip": "127.0.0.1",
        "port": "53",
    }
    assert relation_harness.model.unit.status.name == "active"


def test_dns_provider_relation_created_no_ingress_address(harness):
    # The harness fixture does not have the ingress address
    # in its relation data by default,
    # so it will be missing
    harness.add_relation("dns-provider", "kubernetes-master")
    assert harness.model.unit.status.name == "maintenance"


def test_dns_provider_relation_created_not_running(
    relation_harness, relation_with_ingress, inactive_service, mocker
):
    container = relation_harness.model.unit.get_container("coredns")
    container.get_service = mocker.MagicMock(return_value=inactive_service)
    relation_harness.begin_with_initial_hooks()
    assert relation_harness.model.unit.status.name == "waiting"


def test_domain_changed(
    relation_harness, relation_with_ingress, active_service, mocker
):
    container = relation_harness.model.unit.get_container("coredns")
    container.get_service = mocker.MagicMock(return_value=active_service)
    container.push = mocker.MagicMock()
    relation_harness.begin_with_initial_hooks()
    domain = "some.domain"
    relation_harness.update_config({"domain": domain})

    # Ensure the new domain name is present in the relation data after the
    # config is updated
    assert relation_harness.get_relation_data(relation_with_ingress, "coredns/0") == {
        "ingress-address": "127.0.0.1",
        "domain": domain,
        "sdn-ip": "127.0.0.1",
        "port": "53",
    }
