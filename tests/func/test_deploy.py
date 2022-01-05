from pathlib import Path
import logging

import pytest
from lightkube import Client, codecs
from lightkube.resources.core_v1 import Pod, Service
import yaml

CHARM_DIR = Path(__file__).parent.parent.parent.resolve()
SPEC_FILE = Path(__file__).parent / "validate-dns-spec.yaml"
META_FILE = Path(__file__).parent.parent.parent / "metadata.yaml"
LOGGER = logging.getLogger(__name__)


async def test_build_and_deploy(ops_test):
    coredns = await ops_test.build_charm(CHARM_DIR)
    LOGGER.info(f"coredns charm built @ {coredns}")
    metadata = yaml.safe_load(META_FILE.read_text())
    upstream_image = metadata["resources"]["coredns-image"]["upstream-source"]

    await ops_test.model.deploy(
        coredns,
        resources={"coredns-image": upstream_image},
        config=dict(forward="8.8.8.8"),
    )
    LOGGER.info("coredns charm deploying...")
    await ops_test.model.wait_for_idle()
    model = ops_test.model_name
    LOGGER.info(f"model {model} is idle")

    client = Client()
    pods = client.list(
        Pod, namespace=model, labels={"app.kubernetes.io/name": "coredns"}
    )
    assert all(
        c.ready for p in pods for c in p.status.containerStatuses
    ), "Not all coredns containers ready"
    LOGGER.info("coredns pod is ready...")


@pytest.fixture()
def coredns_ip(ops_test):
    client = Client()
    coredns_service = client.get(Service, "coredns", namespace=ops_test.model_name)
    yield coredns_service.spec.clusterIP


@pytest.fixture()
def validate_dns_pod(ops_test):
    spec = codecs.load_all_yaml(SPEC_FILE.read_text())
    client = Client()
    for obj in spec:
        client.create(obj)

    client.wait(
        Pod, "validate-dns", namespace="default", for_conditions=("ContainersReady",)
    )

    for obj in spec:
        client.delete(type(obj), obj.metadata.name)


async def test_validate_dns(ops_test, validate_dns_pod, coredns_ip):
    for name, found in (
        ("www.ubuntu.com", True),  # Should find an answer
        ("kubernetes.default.svc.cluster.local", False),
    ):  # Shouldn't find answer
        rc, stdout, stderr = await ops_test.run(
            "kubectl", "exec", "validate-dns", "--", "nslookup", name, coredns_ip
        )
        assert (
            "Non-authoritative answer" in stdout
        ) == found, f"stdout: {stdout}\n stderr: {stderr}"
