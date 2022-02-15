from pathlib import Path
import logging
import pytest
from lightkube import Client, codecs
from lightkube.resources.core_v1 import Pod, Service
import yaml
from tenacity import retry, wait_exponential, stop_after_delay, before_log
from juju.tag import untag

CHARM_DIR = Path(__file__).parent.parent.parent.resolve()
SPEC_FILE = Path(__file__).parent / "data/validate-dns-spec.yaml"
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


async def test_cross_model_relation(ops_test, client_model):
    base_path = Path(__file__).parent
    relation_charm_path = base_path / "data/dns-provider-test"
    relation_charm = await ops_test.build_charm(relation_charm_path)
    relation_charm_resources = {"httpbin-image": "kennethreitz/httpbin"}
    relation_app = await client_model.deploy(
        relation_charm, resources=relation_charm_resources
    )

    await client_model.block_until(
        lambda: len(relation_app.units) == 1, timeout=10 * 60
    )
    # Test app goes to waiting status until the relation is created
    await client_model.wait_for_idle(
        status="waiting", raise_on_blocked=True, timeout=300
    )

    offer, saas, relation = None, None, None
    try:
        logger.info("Creating CMR offer")
        offer = await ops_test.model.create_offer("coredns:dns-provider")
        model_owner = untag("user-", ops_test.model.info.owner_tag)
        logger.info("Consuming CMR offer")
        saas = await client_model.consume(
            f"{model_owner}/{ops_test.model_name}.coredns"
        )
        logger.info("Relating to CMR offer")
        relation = await relation_app.add_relation(
            "dns-provider", "coredns:dns-provider"
        )
        # Once the relation is added, then the test app will go to active status
        await client_model.wait_for_idle(status="active", timeout=60)
        logger.info("Checking Relation Data")
        for unit in relation_app.units:
            action = await unit.run_action("get-relation-data")
            output = await action.wait()
            assert output.status == "completed"
            logger.info("Action Results: ")
            logger.info(action.results)
            assert action.results["domain"] == "cluster.local"
            assert action.results["port"] == "53"
            assert action.results["sdn-ip"] is not None
        logger.info("Relation Data is OK")
    finally:
        if not ops_test.keep_client_model:
            try:
                if relation:
                    logger.info("Cleaning up client relation")
                    await relation_app.remove_relation(
                        "dns-provider", "coredns:dns-provider"
                    )
                    await client_model.wait_for_idle(raise_on_blocked=False, timeout=60)
                    await ops_test.model.wait_for_idle(timeout=60)
                if saas:
                    logger.info("Removing CMR consumer")
                    await client_model.remove_saas("coredns")
                if offer:
                    logger.info("Removing CMR offer")
                    await ops_test.model.remove_offer("coredns")
            except Exception:
                logger.exception("Error performing cleanup")
