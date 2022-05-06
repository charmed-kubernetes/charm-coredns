#!/usr/bin/env python3

import logging
from string import Template
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus, ModelError
from ops.pebble import ServiceStatus
from ops.pebble import Error as PebbleError
from pathlib import Path
from lightkube.models.core_v1 import ServicePort
from lightkube import Client, codecs

logger = logging.getLogger(__name__)


class CoreDNSCharm(CharmBase):
    """CoreDNS Sidecar Charm"""

    _COREDNS_CONTAINER = "coredns"

    def __init__(self, *args):
        super().__init__(*args)

        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            self.model.unit.status = WaitingStatus("Waiting for leadership")
            return

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

    @property
    def is_running(self):
        """Determine if a given service is running in a given container"""
        try:
            container = self.unit.get_container(self._COREDNS_CONTAINER)
            service = container.get_service(self._COREDNS_CONTAINER)
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
        container.add_layer(self._COREDNS_CONTAINER, layer)
        self._push_corefile_config(event)
        self._apply_rbac_policy(event)
        container.autostart()
        self._on_update_status(event)

    def _on_config_changed(self, event):
        """Process charm config changes and restart CoreDNS workload"""
        container = self.unit.get_container(self._COREDNS_CONTAINER)
        if not self.is_running:
            logger.info("CoreDNS is not running")
            return

        self._push_corefile_config(event)
        self._apply_rbac_policy(event)
        container.stop(self._COREDNS_CONTAINER)
        container.start(self._COREDNS_CONTAINER)

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
        else:
            self.unit.status = ActiveStatus()

    def _coredns_layer(self):
        """Pebble config layer for CoreDNS"""
        return {
            "summary": "CoreDNS layer",
            "description": "pebble config layer for CoreDNS",
            "services": {
                self._COREDNS_CONTAINER: {
                    "override": "replace",
                    "summary": "CoreDNS",
                    "command": "/coredns -conf /etc/coredns/Corefile",
                    "startup": "enabled",
                }
            },
        }

    def _push_corefile_config(self, event):
        """Push corefile config to CoreDNS container"""
        container = self.unit.get_container(self._COREDNS_CONTAINER)
        corefile = Template(self.model.config["corefile"])
        corefile = corefile.safe_substitute(self.model.config)
        container.push("/etc/coredns/Corefile", corefile, make_dirs=True)

    def _apply_rbac_policy(self, _event):
        if not self.unit.is_leader():
            return
        client = Client(field_manager="lightkube", namespace=self.model.name)
        with Path("files", "rbac-policy.yaml").open() as f:
            for policy in codecs.load_all_yaml(f):
                if policy.kind == "ClusterRoleBinding":
                    for subject in policy.subjects:
                        subject.namespace = self.model.name
                client.apply(policy)


if __name__ == "__main__":
    main(CoreDNSCharm)
