import unittest.mock as mock

import pytest
from lightkube.models.core_v1 import Service
from lightkube.models.meta_v1 import ObjectMeta
from ops.manifests import (
    HashableResource,
    ManifestClientError,
    ResourceAnalysis,
)
from ops.testing import Harness


def test_action_list_versions(harness: Harness):
    harness.begin()
    action_event = harness.run_action("list-versions")
    versions = action_event.results["coredns-versions"].splitlines()
    assert sorted(versions) == [
        "v1.12.0",
        "v1.12.1",
        "v1.13.1",
    ]


@pytest.mark.parametrize(
    "config_namespace,expected_namespace",
    [
        ("kube-system", "kube-system"),
        ("{model}", "test-model"),
    ],
)
def test_action_list_resources(
    harness: Harness, lk_client, api_error_klass, config_namespace, expected_namespace
):
    harness.begin()
    harness.update_config({"coredns_namespace": config_namespace})
    not_found = api_error_klass()
    not_found.status.code = 404
    not_found.status.message = "Not Found"
    lk_client.get.side_effect = not_found

    model = harness.model.name
    uuid = harness.model.uuid[:8]
    matching_ns = config_namespace == expected_namespace

    expected_results = {
        "coredns-missing": "\n".join(
            [
                "ClusterRole/juju:{}-{}:coredns".format(model, uuid),
                "ClusterRoleBinding/juju:{}-{}:coredns".format(model, uuid),
                f"ConfigMap/{expected_namespace}/coredns",
                f"Deployment/{expected_namespace}/coredns",
                f"Service/{expected_namespace}/kube-dns",
                f"ServiceAccount/{expected_namespace}/coredns" if matching_ns else "",
            ]
        ).strip(),
    }
    action = harness.run_action("list-resources", {})
    assert action.results == expected_results

    action = harness.run_action("scrub-resources", {})
    assert action.results == expected_results

    action = harness.run_action("sync-resources", {})
    assert action.results == expected_results


def test_action_sync_resources_install_failure(harness, lk_client, api_error_klass):
    harness.begin()
    not_found = api_error_klass()
    not_found.status.code = 404
    not_found.status.message = "Not Found"
    lk_client.get.side_effect = not_found

    lk_client.apply.side_effect = ManifestClientError("API Server Unavailable")
    action = harness.run_action("sync-resources", {})

    lk_client.delete.assert_not_called()
    assert (
        action.results["result"]
        == "Failed to sync missing resources: API Server Unavailable"
    )


@pytest.fixture()
def update_status_charm(harness):
    harness.set_leader(True)
    harness.begin()
    rel = harness.add_relation("dns-provider", "kubernetes-control-plane")
    harness.update_relation_data(rel, "coredns/0", {"ingress-address": "127.0.0.1"})
    harness.charm.reconciler.stored.reconciled = True
    with mock.patch.object(
        harness.charm.manifest, "get_service_address"
    ) as mock_service_address:
        mock_service_address.return_value = "10.185.10.11"
        yield harness.charm


def test_update_status_unready(update_status_charm):
    with mock.patch.object(update_status_charm, "collector") as mock_collector:
        mock_collector.unready = ["not-ready"]
        update_status_charm.on.update_status.emit()
    assert update_status_charm.unit.status.name == "waiting"
    assert update_status_charm.unit.status.message == "not-ready"


def test_update_status_no_address(update_status_charm):
    with mock.patch.object(
        update_status_charm.manifest, "get_service_address"
    ) as mock_service_address:
        mock_service_address.return_value = ""
        update_status_charm.on.update_status.emit()
    assert update_status_charm.unit.status.name == "waiting"
    assert update_status_charm.unit.status.message == "Waiting for DNS service address"


def test_update_status_ready(update_status_charm):
    update_status_charm.stored.namespace = "default"
    with mock.patch.object(update_status_charm, "collector") as mock_collector:
        mock_collector.unready = []
        mock_collector.short_version = "short-version"
        mock_collector.long_version = "long-version"
        update_status_charm.on.update_status.emit()
    assert update_status_charm.unit.status.name == "active"
    assert update_status_charm.app._backend._workload_version == "short-version"
    assert update_status_charm.app.status.message == "long-version"
    rel = update_status_charm.model.get_relation("dns-provider")
    expected = {
        ("sdn-ip", "10.185.10.11"),
        ("port", "53"),
        ("domain", update_status_charm.model.config["domain"]),
    }
    assert expected.issubset(set(rel.data[update_status_charm.unit].items()))


def test_reconcile_through_evaluate_manifests(harness):
    harness.begin()
    harness.enable_hooks()
    failure = "Failed to evaluate manifests"
    with mock.patch.object(harness.charm.manifest, "evaluate") as evaluation:
        evaluation.return_value = failure
        harness.set_leader(True)
        evaluation.assert_called_once_with()
    assert harness.charm.unit.status.name == "blocked"
    assert harness.charm.unit.status.message == failure


def test_reconcile_through_prevent_collisions(harness):
    harness.begin()
    harness.enable_hooks()

    conflicts = HashableResource(
        Service(metadata=ObjectMeta(name="coredns", namespace="kube-system"))
    )
    expected_results = [
        ResourceAnalysis("coredns", {conflicts}, set(), set(), set()),
    ]

    with mock.patch.object(harness.charm.collector, "analyze_resources") as analysis:
        analysis.return_value = expected_results
        harness.set_leader(True)
        analysis.assert_called_once()
    assert harness.charm.unit.status.name == "blocked"
    assert (
        harness.charm.unit.status.message
        == "1 Kubernetes resource collision (action: list-resources)"
    )


def test_reconcile_through_install_manifests(harness):
    harness.begin()
    harness.enable_hooks()

    with (
        mock.patch.object(harness.charm.collector, "analyze_resources") as analysis,
        mock.patch.object(harness.charm.manifest, "apply_manifests") as apply,
    ):
        analysis.return_value = []
        apply.side_effect = ManifestClientError("Map", "Failure", "Errors")
        harness.set_leader(True)
        apply.assert_called_once()
    assert harness.charm.unit.status.name == "waiting"
    assert harness.charm.unit.status.message == "Map -> Failure -> Errors"


def test_reconcile_through_update_status(harness):
    harness.begin()
    harness.enable_hooks()

    with (
        mock.patch.object(harness.charm.collector, "analyze_resources") as analysis,
        mock.patch.object(harness.charm, "_update_status") as update_status,
        mock.patch.object(harness.charm.manifest, "apply_manifests"),
    ):
        analysis.return_value = []
        harness.set_leader(True)
        update_status.assert_called_once_with()
    assert harness.charm.unit.status.name == "active"
    assert harness.charm.unit.status.message == "Ready"


def test_reconcile_through_destroying(harness):
    harness.begin()
    harness.set_leader(True)
    harness.enable_hooks()

    harness.charm.on.stop.emit()
    assert harness.charm.unit.status.name == "blocked"
    assert harness.charm.unit.status.message == "Removing CoreDNS"
