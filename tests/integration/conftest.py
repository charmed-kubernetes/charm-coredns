import pytest_asyncio
import asyncio
from random import choices
from string import ascii_lowercase, digits
import juju.model
import logging
from pathlib import Path
from juju.tag import untag

logger = logging.getLogger(__name__)


def pytest_addoption(parser):
    parser.addoption(
        "--client-model",
        action="store",
        help="Name of client model to use; if not provided, will "
        "create one and clean it up after.",
    )
    parser.addoption(
        "--keep-client-model",
        action="store_true",
        help="Flag to keep the client model, if automatically created.",
    )


@pytest_asyncio.fixture
async def app_name():
    return "coredns"


@pytest_asyncio.fixture
async def client_model(ops_test, request):
    # TODO: fold this into pytest-operator
    model_name = request.config.option.client_model
    if not model_name:
        ops_test.keep_client_model = request.config.option.keep_client_model
        module_name = request.module.__name__.rpartition(".")[-1]
        suffix = "".join(choices(ascii_lowercase + digits, k=4))
        model_name = f"{module_name.replace('_', '-')}-client-{suffix}"
        if not ops_test._controller:
            ops_test._controller = juju.model.Controller()
            await ops_test._controller.connect(ops_test.controller_name)
        model = await ops_test._controller.add_model(
            model_name, cloud_name=ops_test.cloud_name
        )
        # NB: This call to `juju models` is needed because libjuju's
        # `add_model` doesn't update the models.yaml cache that the Juju
        # CLI depends on with the model's UUID, which the CLI requires to
        # connect. Calling `juju models` beforehand forces the CLI to
        # update the cache from the controller.
        await ops_test.juju("models")
    else:
        ops_test.keep_client_model = True
        model = juju.model.Model()
        await model.connect(model_name)
    try:
        yield model
    finally:
        if not ops_test.keep_client_model:
            try:
                await asyncio.gather(
                    *(app.remove() for app in model.applications.values())
                )
                await model.block_until(lambda: not model.applications, timeout=2 * 60)
            except asyncio.TimeoutError:
                logger.error("Timed out cleaning up client model")
            except Exception:
                logger.exception("Error cleanup in client model")
        await model.disconnect()
        if not ops_test.keep_client_model:
            await ops_test._controller.destroy_model(model_name)


@pytest_asyncio.fixture
async def coredns_test_app(ops_test, client_model):
    base_path = Path(__file__).parent
    charm_path = base_path / "data/dns-provider-test"
    charm = await ops_test.build_charm(charm_path)
    charm_resources = {"httpbin-image": "kennethreitz/httpbin"}
    app = await client_model.deploy(charm, resources=charm_resources)

    await client_model.block_until(lambda: len(app.units) == 1, timeout=10 * 60)
    # Test app goes to waiting status until the relation is created
    await client_model.wait_for_idle(
        status="waiting", raise_on_blocked=True, timeout=300
    )
    return app


@pytest_asyncio.fixture
async def related_app(ops_test, client_model, coredns_test_app, app_name):
    offer, saas, relation = None, None, None
    logger.info("Creating CMR offer")
    offer = await ops_test.model.create_offer(f"{app_name}:dns-provider")
    model_owner = untag("user-", ops_test.model.info.owner_tag)
    logger.info("Consuming CMR offer")
    saas = await client_model.consume(f"{model_owner}/{ops_test.model_name}.{app_name}")
    logger.info("Relating to CMR offer")
    relation = await coredns_test_app.add_relation(
        "dns-provider", f"{app_name}:dns-provider"
    )
    yield coredns_test_app
    # Clean up
    if not ops_test.keep_client_model:
        try:
            if relation:
                logger.info("Cleaning up client relation")
                await coredns_test_app.remove_relation(
                    "dns-provider", f"{app_name}:dns-provider"
                )
                await client_model.wait_for_idle(raise_on_blocked=False, timeout=60)
                await ops_test.model.wait_for_idle(timeout=60)
            if saas:
                logger.info("Removing CMR consumer")
                await client_model.remove_saas(app_name)
            if offer:
                logger.info("Removing CMR offer")
                await ops_test.model.remove_offer(app_name)
        except Exception:
            logger.exception("Error performing cleanup")
