from pathlib import Path
import logging
import pytest
from lightkube import Client, codecs
from lightkube.resources.core_v1 import Pod, Service
import yaml
from tenacity import retry, wait_exponential, stop_after_delay, before_log

CHARM_DIR = Path(__file__).parent.parent.parent.resolve()
SPEC_FILE = Path(__file__).parent / "data" / "validate-dns-spec.yaml"
META_FILE = Path(__file__).parent.parent.parent / "metadata.yaml"
logger = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test):
    app_name = "coredns"
    coredns = await ops_test.build_charm(CHARM_DIR)
    logger.info(f"CoreDNS charm built @ {coredns}")
    metadata = yaml.safe_load(META_FILE.read_text())
    upstream_image = metadata["resources"]["coredns-image"]["upstream-source"]

    await ops_test.model.deploy(
        coredns,
        resources={"coredns-image": upstream_image},
        config=dict(forward="8.8.8.8"),
        application_name=app_name,
        trust=True,
    )
    logger.info("Deploying CoreDNS charm ...")
    await ops_test.model.wait_for_idle(apps=[app_name], status="active")
    await ops_test.model.block_until(
        lambda: len(ops_test.model.applications[app_name].units) > 0
    )
    model_name = ops_test.model_name
    logger.info(f"model {model_name} is idle")

    assert ops_test.model.applications[app_name].units[0].workload_status == "active"

    # Wait for pod to be ready
    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_delay(120),
        reraise=True,
        before=before_log(logger, logging.INFO),
    )
    def wait_for_ready():
        client = Client()
        pods = client.list(
            Pod, namespace=model_name, labels={"app.kubernetes.io/name": "coredns"}
        )
        assert all(
            c.ready for p in pods for c in p.status.containerStatuses
        ), "Not all coredns containers ready"

    wait_for_ready()
    logger.info("CoreDNS pod is ready ...")


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


async def test_cross_model_relation(ops_test, related_app, client_model):
    # After the app is related, it should reach active status if the
    # relation data is present
    logger.info("Waiting for active status ...")
    await client_model.wait_for_idle(status="active", timeout=60)
    unit = client_model.applications["dns-provider-test"].units[0]
    assert unit.workload_status == "active"
