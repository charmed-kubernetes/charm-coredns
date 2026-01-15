#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Dispatch logic for the CoreDNS operator charm."""

import logging
from typing import List, cast

import charms.contextual_status as status
import ops
from charms.reconciler import Reconciler
from ops.manifests import Collector, ManifestClientError, Manifests, ResourceAnalysis

from coredns_manifests import CoreDNSManifests

logger = logging.getLogger(__name__)


class CoreDNSCharm(ops.CharmBase):
    """Dispatch logic for the CoreDNS operator charm."""

    stored = ops.StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self.reconciler = Reconciler(self, self.reconcile)

        self.framework.observe(self.on.list_versions_action, self._list_versions)
        self.framework.observe(self.on.list_resources_action, self._list_resources)
        self.framework.observe(self.on.scrub_resources_action, self._scrub_resources)
        self.framework.observe(self.on.sync_resources_action, self._sync_resources)
        self.framework.observe(self.on.update_status, self._on_update_status)

        # hashed value of the provider config once valid
        self.stored.set_default(config_hash=0)
        # whether the manifests are deployed
        self.stored.set_default(deployed=False)
        # whether the charm is being destroyed
        self.stored.set_default(destroying=False)

        self.manifest = CoreDNSManifests(self)
        self.collector = Collector(self.manifest)

    def _list_versions(self, event: ops.ActionEvent) -> None:
        self.collector.list_versions(event)

    def _list_resources(self, event: ops.ActionEvent) -> None:
        manifests = event.params.get("manifest", "")
        resources = event.params.get("resources", "")
        self.collector.list_resources(event, manifests, resources)

    def _scrub_resources(self, event: ops.ActionEvent) -> None:
        manifests = event.params.get("manifest", "")
        resources = event.params.get("resources", "")
        self.collector.scrub_resources(event, manifests, resources)

    def _sync_resources(self, event: ops.ActionEvent) -> None:
        manifests = event.params.get("manifest", "")
        resources = event.params.get("resources", "")
        try:
            self.collector.apply_missing_resources(event, manifests, resources)
        except ManifestClientError as e:
            msg = "Failed to sync missing resources: "
            msg += " -> ".join(map(str, e.args))
            event.set_results({"result": msg})
        else:
            self.stored.deployed = True

    def _update_status(self) -> None:
        address = self.manifest.get_service_address()
        self._provide_kube_dns(address)
        if unready := self.collector.unready:
            status.add(ops.WaitingStatus(", ".join(unready)))
            raise status.ReconcilerError("Waiting for deployment")
        elif not address:
            status.add(ops.WaitingStatus("Waiting for DNS service address"))
            raise status.ReconcilerError("No service address")
        else:
            self.unit.set_workload_version(self.collector.short_version)
            if self.unit.is_leader():
                self.app.status = ops.ActiveStatus(self.collector.long_version)

    def _on_update_status(self, _: ops.EventBase) -> None:
        if not self.reconciler.stored.reconciled:
            return
        try:
            with status.context(self.unit):
                self._update_status()
        except status.ReconcilerError:
            logger.exception("Can't update_status")

    def _provide_kube_dns(self, cluster_address: str) -> None:
        """Provide DNS info to the dns-provider relation."""
        for rel in self.model.relations.get("dns-provider", []):
            try:
                rel.data[self.unit].update(
                    **{
                        "domain": self.model.config["domain"],
                        "sdn-ip": str(cluster_address),
                        "port": "53",
                    }
                )
            except ops.ModelError as e:
                logger.error("Failed to set dns-provider relation data: %s", e)

    def reconcile(self, event: ops.EventBase) -> None:
        """Reconcile the charm state."""
        if self._destroying(event):
            leader = self.unit.is_leader()
            logger.info("purge manifests if leader(%s) event(%s)", leader, event)
            if leader:
                self._purge_all_manifests()
            status.add(ops.BlockedStatus("Removing CoreDNS"))
            return
        hasher = self.evaluate_manifests()
        self.prevent_collisions(event)
        self.install_manifests(config_hash=hasher)
        self._update_status()

    def evaluate_manifests(self) -> int:
        """Evaluate all manifests."""
        self.unit.status = ops.MaintenanceStatus("Evaluating CoreDNS")
        if evaluation := self.manifest.evaluate():
            status.add(ops.BlockedStatus(evaluation))
            raise status.ReconcilerError(evaluation)
        return self.manifest.hash()

    def prevent_collisions(self, event: ops.EventBase) -> None:
        """Prevent manifest collisions."""
        if self.unit.is_leader():
            self.unit.status = ops.MaintenanceStatus("Detecting manifest collisions")
            analyses: List[ResourceAnalysis] = self.collector.analyze_resources(
                event, "", ""
            )
            count = sum(len(a.conflicting) for a in analyses)
            if count > 0:
                msg = f"{count} Kubernetes resource collision{'s' if count != 1 else ''} (action: list-resources)"
                logger.error(msg)
                for analysis in analyses:
                    if analysis.conflicting:
                        logger.error(
                            " Collision count in '%s' is %d",
                            analysis.manifest,
                            len(analysis.conflicting),
                        )
                        for _ in sorted(map(str, analysis.conflicting)):
                            logger.error("   %s", _)
                status.add(ops.BlockedStatus(msg))
                raise status.ReconcilerError(msg)

    def install_manifests(self, config_hash: int) -> None:
        """Install or update manifests as needed."""
        if cast(int, self.stored.config_hash) == config_hash:
            logger.info(f"No config changes detected. config_hash={config_hash}")
            return
        if self.unit.is_leader():
            self.unit.status = ops.MaintenanceStatus("Deploying CoreDNS")
            self.unit.set_workload_version("")
            for manifest in self.collector.manifests.values():
                try:
                    manifest.apply_manifests()
                except ManifestClientError as e:
                    failure_msg = " -> ".join(map(str, e.args))
                    status.add(ops.WaitingStatus(failure_msg))
                    logger.warning("Encountered retriable installation error: %s", e)
                    raise status.ReconcilerError(failure_msg)

        self.stored.config_hash = config_hash

    def _purge_all_manifests(self) -> None:
        """Purge resources created by this charm."""
        self.unit.status = ops.MaintenanceStatus("Removing Kubernetes resources")
        for manifest in self.collector.manifests.values():
            self._purge_manifest(manifest)
        self.stored.config_hash = 0

    @status.on_error(ops.WaitingStatus("Manifest purge failed."))
    def _purge_manifest(self, manifest: Manifests) -> None:
        """Purge resources created by this charm by manifest."""
        manifest.delete_manifests(ignore_unauthorized=True, ignore_not_found=True)

    def _destroying(self, event: ops.EventBase) -> bool:
        """Check if the charm is being destroyed."""
        if cast(bool, self.stored.destroying):
            return True
        if isinstance(event, (ops.StopEvent, ops.RemoveEvent)):
            self.stored.destroying = True
            return True
        return False


if __name__ == "__main__":
    ops.main(CoreDNSCharm)
