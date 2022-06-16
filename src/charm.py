#!/usr/bin/env python3

import logging
from string import Template
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from ops.charm import CharmBase
from ops.charm import CharmMeta
from ops.framework import StoredState
from ops.main import main
from ops.model import (
    BlockedStatus,
    ActiveStatus,
    WaitingStatus,
    MaintenanceStatus,
    ModelError,
)
from ops.pebble import ServiceStatus
from ops.pebble import Error as PebbleError
from pathlib import Path
from lightkube.models.core_v1 import ServicePort
from lightkube.resources.apps_v1 import StatefulSet
from lightkube import Client, codecs, ApiError
from typing import Optional

logger = logging.getLogger(__name__)


def _get_metadata():
    root_path = Path(__file__).parent.parent
    metadata_path = root_path / "metadata.yaml"
    with metadata_path.open() as f:
        return CharmMeta.from_yaml(f)


class CoreDNSCharm(CharmBase):
    """CoreDNS Sidecar Charm"""

    _stored = StoredState()

    _CHARM_NAME = _get_metadata().name

    def __init__(self, *args):
        super().__init__(*args)

        self.client = Client(field_manager=self.model.name, namespace=self.model.name)
        dns_udp = ServicePort(53, protocol="UDP", name="dns")
        dns_tcp = ServicePort(53, protocol="TCP", name="dns-tcp")
        metrics = ServicePort(9153, protocol="TCP", name="metrics")
        self.service_patcher = KubernetesServicePatch(self, [dns_udp, dns_tcp, metrics])

        self.framework.observe(
            self.on.coredns_pebble_ready, self._on_coredns_pebble_ready
        )
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(
            self.on.dns_provider_relation_created,
            self._on_dns_provider_relation_created,
        )
        self.framework.observe(self.on.update_status, self._on_update_status)
        self._stored.set_default(forbidden=False)

    @property
    def is_running(self):
        """Determine if a given service is running in a given container"""
        try:
            container = self.unit.get_container(self._CHARM_NAME)
            service = container.get_service(self._CHARM_NAME)
        except (ModelError, PebbleError):
            return False
        return service.current == ServiceStatus.ACTIVE

    def _on_coredns_pebble_ready(self, event):
        """Define and start CoreDNS workload"""
        container = event.workload
        if self.is_running:
            logger.info("CoreDNS already started")
            return

        layer = self._coredns_layer()
        container.add_layer(self._CHARM_NAME, layer, combine=True)
        self._push_corefile_config(event)
        self._apply_rbac_policy(event)
        self._patch_statefulset()
        container.autostart()
        self._on_update_status(event)

    def _on_config_changed(self, event):
        """Process charm config changes and restart CoreDNS workload"""
        container = self.unit.get_container(self._CHARM_NAME)
        if not self.is_running:
            logger.info("CoreDNS is not running")
            return

        self._push_corefile_config(event)
        self._apply_rbac_policy(event)
        container.stop(self._CHARM_NAME)
        container.start(self._CHARM_NAME)

        # Update the domain data in the relation in case the domain changed
        if self.unit.is_leader():
            relation = self.model.get_relation("dns-provider")
            if relation is not None:
                provided_data = self.model.get_relation("dns-provider").data[self.unit]
                provided_data.update(
                    {
                        "domain": self.model.config["domain"],
                    }
                )

        self._on_update_status(event)

    def _on_dns_provider_relation_created(self, event):
        """Provide relation data on dns-provider relation created"""
        if self.unit.is_leader():
            ingress_address = event.relation.data[self.unit].get("ingress-address")
            if not ingress_address:
                logger.info(
                    "ingress-address is not present in relation data, deferring"
                )
                self.unit.status = MaintenanceStatus("Waiting on ingress-address")
                event.defer()
                return
            data = event.relation.data[self.unit]
            data.update(
                {
                    "domain": self.model.config["domain"],
                    "sdn-ip": str(ingress_address),
                    "port": "53",
                }
            )
        self._on_update_status(event)

    def _on_update_status(self, event):
        """Update Juju status"""
        if not self.is_running:
            self.unit.status = WaitingStatus("CoreDNS is not running")
        elif self._stored.forbidden:
            self.unit.status = BlockedStatus("Forbidden to apply RBAC Policies.")
        else:
            self.unit.status = ActiveStatus()

    def _coredns_layer(self):
        """Pebble config layer for CoreDNS"""
        return {
            "summary": "CoreDNS layer",
            "description": "pebble config layer for CoreDNS",
            "services": {
                self._CHARM_NAME: {
                    "override": "replace",
                    "summary": "CoreDNS",
                    "command": "/coredns -conf /etc/coredns/Corefile",
                    "startup": "enabled",
                }
            },
        }

    def _push_corefile_config(self, event):
        """Push corefile config to CoreDNS container"""
        container = self.unit.get_container(self._CHARM_NAME)
        corefile = Template(self.model.config["corefile"])
        corefile = corefile.safe_substitute(self.model.config)
        container.push("/etc/coredns/Corefile", corefile, make_dirs=True)

    def _apply_rbac_policy(self, _event) -> Optional[str]:
        if not self.unit.is_leader():
            return
        logger.info("Applying RBAC policies")
        client = Client(field_manager=self.model.name, namespace=self.model.name)
        self._stored.forbidden = False
        with Path("files", "rbac-policy.yaml").open() as f:
            for policy in codecs.load_all_yaml(f):
                if policy.kind == "ClusterRoleBinding":
                    for subject in policy.subjects:
                        subject.namespace = self.model.name
                try:
                    client.apply(policy)
                except ApiError as err:
                    self._stored.forbidden |= err.status.code == 403
                    if not self._stored.forbidden:
                        raise

    def _patch_statefulset(self):
        if not self.unit.is_leader():
            return
        logger.info(f"Patching Default dnsPolicy for {self._CHARM_NAME} statefulset")
        client = Client(field_manager=self.model.name, namespace=self.model.name)
        patch = {"spec": {"template": {"spec": {"dnsPolicy": "Default"}}}}
        client.patch(
            StatefulSet, name=self._CHARM_NAME, namespace=self.model.name, obj=patch
        )


if __name__ == "__main__":
    main(CoreDNSCharm)
