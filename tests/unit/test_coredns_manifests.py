import pytest
import yaml

from coredns_manifests import CoreDNSManifests


@pytest.fixture
def manifest(harness):
    harness.disable_hooks()
    harness.begin()
    yield CoreDNSManifests(harness.charm)


def test_steady_hash(manifest):
    hash1 = manifest.hash()
    hash2 = manifest.hash()
    assert hash1 == hash2


def test_manipulations(harness, manifest):
    harness.update_config({"coredns_namespace": "kube-system"})
    assert len(manifest.resources) == 6, "Include Service Account"
    assert any(_.kind == "ServiceAccount" for _ in manifest.resources)


def test_manipulations_no_sa(harness, manifest):
    harness.update_config({"coredns_namespace": "{model}"})
    assert len(manifest.resources) == 5
    assert not any(_.kind == "ServiceAccount" for _ in manifest.resources)


def test_namespace_config(harness, manifest):
    harness.update_config({"coredns_namespace": "custom-namespace"})
    assert manifest.evaluate() is None
    for resource in manifest.resources:
        if resource.kind in ["ClusterRole", "ClusterRoleBinding"]:
            assert resource.namespace is None
        else:
            assert resource.namespace == "custom-namespace"

    harness.update_config({"coredns_namespace": "{model}"})
    assert manifest.evaluate() is None
    for resource in manifest.resources:
        if resource.kind in ["ClusterRole", "ClusterRoleBinding"]:
            assert resource.namespace is None
        else:
            assert resource.namespace == harness.model.name

    harness.update_config({"coredns_namespace": ""})
    with pytest.raises(KeyError):
        manifest.resources
    assert "coredns_namespace" in manifest.evaluate()


def _get_deployment(manifest):
    """Return the coredns Deployment resource from the manifest."""
    return next(r for r in manifest.resources if r.kind == "Deployment")


def test_topology_spread_constraints_default(harness, manifest):
    """Default config sets a topology spread constraint for node-level distribution."""
    deployment = _get_deployment(manifest)
    tscs = deployment.resource.spec.template.spec.topologySpreadConstraints
    assert tscs, "Expected at least one topology spread constraint by default"
    assert len(tscs) == 1
    tsc = tscs[0]
    assert tsc.maxSkew == 1
    assert tsc.topologyKey == "kubernetes.io/hostname"
    assert tsc.whenUnsatisfiable == "ScheduleAnyway"
    assert tsc.labelSelector is not None
    assert tsc.labelSelector.matchLabels == {"k8s-app": "kube-dns"}


def test_topology_spread_constraints_empty(harness, manifest):
    """Setting topology_spread_constraints to '' disables the feature."""
    harness.update_config({"topology_spread_constraints": ""})
    deployment = _get_deployment(manifest)
    tscs = deployment.resource.spec.template.spec.topologySpreadConstraints
    assert not tscs


def test_topology_spread_constraints_custom(harness, manifest):
    """A custom topology_spread_constraints YAML string is applied correctly."""
    custom = yaml.dump(
        [
            {
                "maxSkew": 2,
                "topologyKey": "topology.kubernetes.io/zone",
                "whenUnsatisfiable": "DoNotSchedule",
                "labelSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ]
    )
    harness.update_config({"topology_spread_constraints": custom})
    deployment = _get_deployment(manifest)
    tscs = deployment.resource.spec.template.spec.topologySpreadConstraints
    assert tscs and len(tscs) == 1
    tsc = tscs[0]
    assert tsc.maxSkew == 2
    assert tsc.topologyKey == "topology.kubernetes.io/zone"
    assert tsc.whenUnsatisfiable == "DoNotSchedule"


def test_topology_spread_constraints_invalid_yaml(harness, manifest):
    """Non-list YAML for topology_spread_constraints is silently ignored."""
    harness.update_config({"topology_spread_constraints": "not-a-list: true"})
    deployment = _get_deployment(manifest)
    # Should not raise; constraints should be None/empty
    tscs = deployment.resource.spec.template.spec.topologySpreadConstraints
    assert not tscs
