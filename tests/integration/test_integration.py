import logging
from pathlib import Path
from typing import Dict

import juju.utils
import pytest
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)
TESTS_DIR = Path(__file__).parent.parent.parent


def _charm_resources(metadata) -> Dict[str, str]:
    """Return a dict of resources defined in charmcraft.yaml."""
    resources = metadata.get("resources", {})
    resource_dict = {}
    for name, value in resources.items():
        image_name = value.get("upstream-source", "")
        if not image_name:
            continue
        resource_dict[name] = image_name
    return resource_dict


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest, coredns_model):
    _, k8s_alias = coredns_model

    charm = next(Path(".").glob("coredns*.charm"), None)
    if not charm:
        log.info("Building Charm...")
        charm = await ops_test.build_charm(".")

    metadata = juju.utils.get_local_charm_metadata(charm)
    resources = _charm_resources(metadata)
    with ops_test.model_context(k8s_alias) as model:
        await model.deploy(
            entity_url=charm.resolve(),
            # Prevent conflicts when deploying on a cluster where coredns
            # is already deployed into the kube-system namespace
            config={"coredns_namespace": "{model}"},
            resources=resources,
            trust=True,
        )

    await model.block_until(lambda: "coredns" in model.applications, timeout=60)
    await model.wait_for_idle(status="active")


@pytest.mark.usefixtures("related", "validate_dns_pod")
class TestResolution:
    async def test_internal_resolution(self, ops_test, k8s_client, coredns_ip):
        _, namespace = k8s_client
        log.info("Testing internal resolution ...")
        rc, stdout, stderr = await ops_test.run(
            "kubectl",
            "exec",
            "validate-dns",
            "-n",
            namespace,
            "--",
            "nslookup",
            "kubernetes.default.svc.cluster.local",
        )
        assert f"Server:\t\t{coredns_ip}" in stdout, (
            f"stdout: {stdout}\n stderr: {stderr}"
        )
        assert "kubernetes.default.svc.cluster.local" in stdout, (
            f"stdout: {stdout}\n stderr: {stderr}"
        )
        assert rc == 0

    async def test_external_resolution(self, ops_test, k8s_client, coredns_ip):
        _, namespace = k8s_client
        log.info("Testing external resolution ...")
        rc, stdout, stderr = await ops_test.run(
            "kubectl",
            "exec",
            "validate-dns",
            "-n",
            namespace,
            "--",
            "nslookup",
            "www.ubuntu.com",
        )
        assert f"Server:\t\t{coredns_ip}" in stdout, (
            f"stdout: {stdout}\n stderr: {stderr}"
        )
        assert "Non-authoritative answer" in stdout, (
            f"stdout: {stdout}\n stderr: {stderr}"
        )
        assert rc == 0
