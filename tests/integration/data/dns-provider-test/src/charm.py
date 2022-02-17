#!/usr/bin/env python3
import logging

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus, ModelError
from ops.pebble import ServiceStatus
from ops.pebble import Error as PebbleError

logger = logging.getLogger(__name__)


class DnsProviderTestCharm(CharmBase):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(*args)
        self.is_related = False
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(
            self.on.httpbin_pebble_ready, self._on_httpbin_pebble_ready
        )
        self.framework.observe(
            self.on.dns_provider_relation_changed,
            self._on_dns_provider_relation_changed,
        )

    @property
    def is_running(self):
        """Determine if a given service is running in a given container"""
        try:
            container = self.unit.get_container("httpbin")
            service = container.get_service("httpbin")
        except (ModelError, PebbleError):
            return False
        return service.current == ServiceStatus.ACTIVE

    def _on_install(self, event):
        if not self.is_running:
            self.unit.status = WaitingStatus("Waiting to start service")

    def _on_httpbin_pebble_ready(self, event):
        # Get a reference the container attribute on the PebbleReadyEvent
        container = event.workload
        # Define an initial Pebble layer configuration
        pebble_layer = {
            "summary": "httpbin layer",
            "description": "pebble config layer for httpbin",
            "services": {
                "httpbin": {
                    "override": "replace",
                    "summary": "httpbin",
                    "command": "gunicorn -b 0.0.0.0:80 httpbin:app -k gevent",
                    "startup": "enabled",
                    "environment": {},
                }
            },
        }
        # Add initial Pebble config layer using the Pebble API
        container.add_layer("httpbin", pebble_layer, combine=True)
        # Autostart any services that were defined with startup: enabled
        container.autostart()
        self._update_status(event)

    def _on_dns_provider_relation_changed(self, event):
        self._update_status(event)

    def _update_status(self, event):
        relation = self.model.get_relation("dns-provider")
        if not relation:
            self.unit.status = WaitingStatus("Awaiting dns-provider relation data")
            return
        data = relation.data[event.unit]
        domain = data.get("domain")
        sdn_ip = data.get("sdn-ip")
        port = data.get("port")
        if None in [domain, sdn_ip, port]:
            self.unit.status = WaitingStatus("Relation is present but data is missing")
        elif not self.is_running:
            self.unit.status = WaitingStatus("DnsProviderTestCharm is not running")
        else:
            self.unit.status = ActiveStatus("DnsProviderTestCharm started")


if __name__ == "__main__":
    main(DnsProviderTestCharm)
