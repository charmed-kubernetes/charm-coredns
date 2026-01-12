import logging
import random
import shlex
import string
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from juju.tag import untag
from lightkube import Client, KubeConfig, codecs
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace, Pod, Service
from kubernetes import config as k8s_config
from kubernetes.client import Configuration
from pytest_operator.plugin import OpsTest


log = logging.getLogger(__name__)


def pytest_addoption(parser):
    parser.addoption(
        "--k8s-cloud",
        action="store",
        help="Juju kubernetes cloud to reuse; if not provided, will create a new cloud",
    )


@pytest_asyncio.fixture(scope="module")
async def charmed_kubernetes(ops_test):
    with ops_test.model_context("main") as model:
        deploy, control_plane_app = True, "kubernetes-control-plane"
        current_model = ops_test.request.config.option.model
        if current_model:
            control_plane_apps = [
                app_name
                for app_name, app in model.applications.items()
                if "kubernetes-control-plane" in app.charm_url
            ]
            if not control_plane_apps:
                pytest.fail(
                    f"Model {current_model} doesn't contain {control_plane_app} charm"
                )
            deploy, control_plane_app = False, control_plane_apps[0]

        if deploy:
            cmd = f"juju deploy -m {ops_test.model_full_name} kubernetes-core --channel=latest/edge"
            await ops_test.run(*shlex.split(cmd), check=True)
            # await model.deploy("kubernetes-core", channel="latest/edge")

        await model.wait_for_idle(status="active", timeout=60 * 60)
        kubeconfig_path = ops_test.tmp_path / "kubeconfig"
        retcode, stdout, stderr = await ops_test.run(
            "juju",
            "scp",
            f"{control_plane_app}/leader:/home/ubuntu/config",
            kubeconfig_path,
        )
        if retcode != 0:
            log.error(f"retcode: {retcode}")
            log.error(f"stdout:\n{stdout.strip()}")
            log.error(f"stderr:\n{stderr.strip()}")
            pytest.fail("Failed to copy kubeconfig from kubernetes-control-plane")
        assert Path(kubeconfig_path).stat().st_size, "kubeconfig file is 0 bytes"
    yield SimpleNamespace(kubeconfig=kubeconfig_path, model=model)


@pytest.fixture(scope="module")
def module_name(request):
    return request.module.__name__.replace("_", "-")


@pytest_asyncio.fixture(scope="module")
async def k8s_client(charmed_kubernetes, request, module_name):
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    namespace = f"{module_name}-{rand_str}"
    config = KubeConfig.from_file(charmed_kubernetes.kubeconfig)
    client = Client(
        config=config.get(context_name="juju-context"),
        namespace=namespace,
        trust_env=False,
    )
    namespace_obj = Namespace(metadata=ObjectMeta(name=namespace))
    log.info(f"Creating namespace {namespace} for use with lightkube client")
    client.create(namespace_obj)
    yield client, namespace
    log.info(f"Deleting namespace {namespace} for use with lightkube client")
    client.delete(Namespace, namespace)


@pytest_asyncio.fixture(scope="module")
async def coredns_model(ops_test: OpsTest, charmed_kubernetes):
    """Create a Juju model into which CoreDNS can be deployed."""
    model_alias = "coredns-model"
    try:
        config = type.__call__(Configuration)
        k8s_config.load_config(
            client_configuration=config, config_file=str(charmed_kubernetes.kubeconfig)
        )
        k8s_cloud = await ops_test.add_k8s(kubeconfig=config, skip_storage=False)
        k8s_model = await ops_test.track_model(
            model_alias, cloud_name=k8s_cloud, keep=ops_test.ModelKeep.NEVER
        )
        yield k8s_model, model_alias
    finally:
        await ops_test.forget_model(model_alias, timeout=10 * 60, allow_failure=True)


@pytest_asyncio.fixture(scope="class")
async def related(ops_test, coredns_model):
    coredns_model_obj, k8s_alias = coredns_model
    app_name = "coredns"
    k8s_cp = ops_test.model.applications["kubernetes-control-plane"]
    machine_model_name = ops_test.model_name
    model_owner = untag("user-", coredns_model_obj.info.owner_tag)
    log.info("Configure dns-provider to none")
    await k8s_cp.set_config({"dns-provider": "none"})

    with ops_test.model_context(k8s_alias) as m:
        offer, saas = None, None
        log.info("Creating CMR offer")
        offer = await m.create_offer(f"{app_name}:dns-provider")
        coredns_model_name = ops_test.model_name

    log.info("Consuming CMR offer")
    log.info(f"{machine_model_name} consuming CMR offer from {coredns_model_name}")
    saas = await ops_test.model.consume(
        f"{model_owner}/{coredns_model_name}.{app_name}"
    )
    log.info("Relating ...")
    await ops_test.model.add_relation(k8s_cp.name, f"{app_name}:dns-provider")
    with ops_test.model_context(k8s_alias) as coredns_model:
        await coredns_model.wait_for_idle(status="active")
    await ops_test.model.wait_for_idle(status="active")
    yield
    with ops_test.model_context(k8s_alias) as m:
        keep = ops_test.keep_model
    if not keep:
        try:
            if saas:
                log.info("Removing CMR consumer")
                await ops_test.model.remove_saas(app_name)
            if offer:
                log.info("Removing CMR offer and relations")
                await coredns_model_obj.remove_offer(
                    f"{coredns_model_name}.{app_name}", force=True
                )
        except Exception:
            log.exception("Error performing cleanup")
        await ops_test.model.wait_for_idle(status="active")


@pytest.fixture(scope="class")
def validate_dns_pod(ops_test, k8s_client):
    client, namespace = k8s_client
    log.info("Creating pod for dns validation ...")
    spec_file = Path(__file__).parent / "data" / "validate-dns-spec.yaml"
    spec = codecs.load_all_yaml(spec_file.read_text())
    log.info("Creating DNS validation pod ...")
    for obj in spec:
        client.create(obj)

    client.wait(
        Pod, "validate-dns", namespace=namespace, for_conditions=("ContainersReady",)
    )
    yield
    log.info("Removing DNS validation pod ...")
    for obj in spec:
        client.delete(type(obj), obj.metadata.name)


@pytest.fixture(scope="class")
def coredns_ip(ops_test, coredns_model, k8s_client):
    _, k8s_alias = coredns_model
    client, _ = k8s_client
    with ops_test.model_context(k8s_alias):
        coredns_service = client.get(Service, "coredns", namespace="kube-system")
    yield coredns_service.spec.clusterIP
