import logging
from pathlib import Path
import pytest
import shlex
import yaml

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test, coredns_model):
    _, k8s_alias = coredns_model
    metadata = yaml.safe_load(Path("metadata.yaml").read_text())
    image = metadata["resources"]["coredns-image"]["upstream-source"]

    with ops_test.model_context(k8s_alias) as m:
        charm = next(Path(".").glob("coredns*.charm"), None)
        if not charm:
            log.info("Building Charm...")
            charm = await ops_test.build_charm(".")

        cmd = (
            f"juju deploy -m {ops_test.model_full_name} "
            f"{charm.resolve()} "
            f"--resource coredns-image={image} "
            "--trust"
        )
        await ops_test.run(*shlex.split(cmd), check=True)

        # await m.deploy(
        #     entity_url=charm.resolve(),
        #     trust=True,
        #     resources={"coredns-image": image},
        # )

        await m.block_until(lambda: "coredns" in m.applications, timeout=60)

        await m.wait_for_idle(status="active")


@pytest.mark.usefixtures("validate_dns_pod", "related")
class DnsTests:
    async def test_internal_resolution(ops_test, k8s_client, coredns_ip):
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
        assert (
            f"Server:\t\t{coredns_ip}" in stdout
        ), f"stdout: {stdout}\n stderr: {stderr}"
        assert (
            "kubernetes.default.svc.cluster.local" in stdout
        ), f"stdout: {stdout}\n stderr: {stderr}"
        assert rc == 0

    async def test_external_resolution(ops_test, k8s_client, coredns_ip):
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
        assert (
            f"Server:\t\t{coredns_ip}" in stdout
        ), f"stdout: {stdout}\n stderr: {stderr}"
        assert (
            "Non-authoritative answer" in stdout
        ), f"stdout: {stdout}\n stderr: {stderr}"
        assert rc == 0
