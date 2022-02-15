#!/usr/bin/env python3
import logging

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, WaitingStatus, ModelError

logger = logging.getLogger(__name__)


class DnsProviderTestCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self._stored.set_default(coredns_relation_data={})
        self.is_related = False
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(
            self.on.httpbin_pebble_ready, self._on_httpbin_pebble_ready
        )
        self.framework.observe(
            self.on.dns_provider_relation_changed,
            self._on_dns_provider_relation_changed,
        )
        self.framework.observe(
            self.on.get_relation_data_action, self._on_get_relation_data_action
        )

    @property
    def is_running(self):
        try:
            container = self.unit.get_container("httpbin")
            return (
                container.can_connect()
                and container.get_service("httpbin").is_running()
            )
        except ModelError:
            return False

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
        self._update_status()

    def _on_dns_provider_relation_changed(self, event):
        provided_data = event.relation.data[event.unit]
        domain = provided_data.get("domain")
        sdn_ip = provided_data.get("sdn-ip")
        port = provided_data.get("port")
        self._stored.coredns_relation_data = {
            "domain": domain,
            "sdn-ip": sdn_ip,
            "port": port,
        }
        self._update_status()

    def _on_get_relation_data_action(self, action):
        if self._stored.coredns_relation_data == {}:
            action.fail("CoreDNS relation data not available")
            return
        action.set_results(self._stored.coredns_relation_data)

    def _update_status(self):
        if self._stored.coredns_relation_data == {}:
            self.unit.status = WaitingStatus("Awaiting dns-provider relation data")
        elif not self.is_running:
            self.unit.status = WaitingStatus("DnsProviderTestCharm is not running")
        else:
            self.unit.status = ActiveStatus("DnsProviderTestCharm started")


if __name__ == "__main__":
    main(DnsProviderTestCharm)
