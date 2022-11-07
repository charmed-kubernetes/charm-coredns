import logging
import os

import juju.utils
from juju.tag import untag
import pytest
import pytest_asyncio
import random
import string
import shlex
from pathlib import Path
from types import SimpleNamespace
import yaml

from lightkube import KubeConfig, Client, codecs
from lightkube.resources.core_v1 import Namespace
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Pod, Service


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
async def k8s_cloud(charmed_kubernetes, ops_test, request, module_name):
    """Use an existing k8s-cloud or create a k8s-cloud
    for deploying a new k8s model into"""
    cloud_name = request.config.option.k8s_cloud or f"{module_name}-k8s-cloud"
    controller = await ops_test.model.get_controller()
    current_clouds = await controller.clouds()
    if f"cloud-{cloud_name}" in current_clouds.clouds:
        yield cloud_name
        return

    with ops_test.model_context("main"):
        log.info(f"Adding cloud '{cloud_name}'...")
        os.environ["KUBECONFIG"] = str(charmed_kubernetes.kubeconfig)
        await ops_test.juju(
            "add-k8s",
            cloud_name,
            "--skip-storage",
            f"--controller={ops_test.controller_name}",
            "--client",
            check=True,
            fail_msg=f"Failed to add-k8s {cloud_name}",
        )
    yield cloud_name

    with ops_test.model_context("main"):
        log.info(f"Removing cloud '{cloud_name}'...")
        await ops_test.juju(
            "remove-cloud",
            cloud_name,
            "--controller",
            ops_test.controller_name,
            "--client",
            check=True,
        )


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
async def coredns_model(k8s_cloud, ops_test):
    model_alias = "coredns-model"
    log.info("Creating CoreDNS model ...")
    model = await ops_test.track_model(
        model_alias, cloud_name=k8s_cloud, credential_name=k8s_cloud
    )
    model_uuid = model.info.uuid
    yield model, model_alias
    timeout = 5 * 60
    await ops_test.forget_model(model_alias, timeout=timeout, allow_failure=False)

    async def model_removed():
        _, stdout, stderr = await ops_test.juju("models", "--format", "yaml")
        if _ != 0:
            return False
        model_list = yaml.safe_load(stdout)["models"]
        which = [m for m in model_list if m["model-uuid"] == model_uuid]
        return len(which) == 0

    log.info("Removing CoreDNS model")
    await juju.utils.block_until_with_coroutine(model_removed, timeout=timeout)
    # Update client's model cache
    await ops_test.juju("models")
    log.info("CoreDNS model removed")


@pytest_asyncio.fixture(scope="module")
async def related(ops_test, coredns_model):
    coredns_model_obj, k8s_alias = coredns_model
    app_name = "coredns"
    machine_model_name = ops_test.model_name
    model_owner = untag("user-", coredns_model_obj.info.owner_tag)
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
    await ops_test.model.add_relation(
        "kubernetes-control-plane", f"{app_name}:dns-provider"
    )
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


@pytest_asyncio.fixture(scope="module")
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


@pytest_asyncio.fixture(scope="module")
def coredns_ip(ops_test, coredns_model, k8s_client):
    coredns_model_obj, k8s_alias = coredns_model
    client, _ = k8s_client
    with ops_test.model_context(k8s_alias):
        coredns_service = client.get(Service, "coredns", namespace=ops_test.model_name)
    yield coredns_service.spec.clusterIP
