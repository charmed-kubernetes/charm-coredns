import pytest

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


@pytest.mark.parametrize("release", ["v1.11.3", "v1.14.2", "v1.14.6"])
def test_release_pin_and_reset(harness, manifest, release):
    for configured, expected in (
        (None, "v1.13.1"),
        (release, release),
        (None, "v1.13.1"),
    ):
        if configured is None:
            harness.update_config(unset=["coredns_release"])
        else:
            harness.update_config({"coredns_release": configured})
        deployment = next(obj for obj in manifest.resources if obj.kind == "Deployment")
        image = deployment.resource.spec.template.spec.containers[0].image
        assert image.rsplit(":", 1)[1] == expected
