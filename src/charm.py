#!/usr/bin/env python3
# Copyright 2021 Ubuntu
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

import logging
from string import Template

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus, ModelError
from ops.pebble import ServiceStatus

logger = logging.getLogger(__name__)


class CharmCoreDNS(CharmBase):
    """CoreDNS Sidecar Charm"""

    _COREDNS_CONTAINER = "coredns"

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.coredns_pebble_ready,
                               self._on_coredns_pebble_ready)
        self.framework.observe(self.on.config_changed,
                               self._on_config_changed)
        self.framework.observe(self.on.dns_provider_relation_changed,
                               self._on_dns_provider_relation_changed)
        self.framework.observe(self.on.update_status,
                               self._on_update_status)

    def _on_coredns_pebble_ready(self, event):
        """Define and start CoreDNS workload"""
        container = event.workload
        if self._is_running(container, self._COREDNS_CONTAINER):
            logger.info("CoreDNS already started")
            return

        layer = self._coredns_layer()
        container.add_layer(self._COREDNS_CONTAINER, layer)
        self._push_corefile_config(event)
        container.autostart()
        self._on_update_status(event)

    def _on_config_changed(self, event):
        """Process charm config changes and restart CoreDNS workload"""
        container = self.unit.get_container(self._COREDNS_CONTAINER)
        if not self._is_running(container, self._COREDNS_CONTAINER):
            logger.info("CoreDNS is not running")
            return

        self._push_corefile_config(event)
        container.stop(self._COREDNS_CONTAINER)
        container.start(self._COREDNS_CONTAINER)
        self._on_update_status(event)

    def _on_dns_provider_relation_changed(self, event):
        """Provide relation data on dns-provider relation"""
        provided_data = event.relation.data[self.unit]
        # TODO(coreycb): Use ingress address instead of bind address
        #                https://pad.lv/1922133
        ingress_address = self.model.get_binding(
            event.relation).network.bind_address
        provided_data.update({
            "domain": self.model.config["domain"],
            "sdn-ip": str(ingress_address),
            "port": "53",
        })
        self._on_update_status(event)

    def _on_update_status(self, event):
        """Update Juju status"""
        container = self.unit.get_container(self._COREDNS_CONTAINER)
        if not self.model.get_relation('dns-provider'):
            self.unit.status = WaitingStatus("Awaiting dns-provider relation")
        elif not self._is_running(container, self._COREDNS_CONTAINER):
            self.unit.status = WaitingStatus("CoreDNS is not running")
        else:
            self.unit.status = ActiveStatus("CoreDNS started")

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

    def _is_running(self, container, service):
        """Determine if a given service is running in a given container"""
        try:
            service = container.get_service(service)
        except ModelError:
            return False
        return service.current == ServiceStatus.ACTIVE


if __name__ == "__main__":
    main(CharmCoreDNS)
