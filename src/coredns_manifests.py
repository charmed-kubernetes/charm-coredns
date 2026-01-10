# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of coredns specific details of the kubernetes manifests."""

import hashlib
import json
import logging
from typing import Dict, Optional, cast
from string import Template

from httpx import HTTPError
from lightkube.resources.core_v1 import Service as ServiceRes
from lightkube.models.core_v1 import ConfigMap, Service
from lightkube.models.apps_v1 import Deployment
from ops.manifests import ConfigRegistry, ManifestLabel, Manifests, Patch

log = logging.getLogger(__file__)
DEPLOYMENT_NAME = "coredns"
SERVICE_NS = "kube-system"
SERVICE_NAME = "kube-dns"


class UpdateConfigMap(Patch):
    """Update the ConfigMap object."""

    def __call__(self, obj):
        """Update the ConfigMap object in the deployment."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == DEPLOYMENT_NAME):
            return
        obj = cast(ConfigMap, obj)
        if obj.data:
            obj.data["Corefile"] = self.corefile

    @property
    def corefile(self) -> str:
        """Return the rendered Corefile configuration."""
        corefile = Template(self.manifests.config["corefile"])
        return corefile.safe_substitute(self.manifests.config)


class UpdateDeployment(Patch):
    """Update the Deployment object."""

    def __call__(self, obj):
        """Update the Deployment object in the deployment."""
        if not (obj.kind == "Deployment" and obj.metadata.name == DEPLOYMENT_NAME):
            return
        obj = cast(Deployment, obj)
        if obj.spec is None or obj.spec.template.spec is None:
            return
        containers = obj.spec.template.spec.containers
        memory_limit = self.manifests.config.get("coredns_memory_limit", "170Mi")
        obj.spec.replicas = self.manifests.config.get("coredns_replicas", 1)
        for container in containers:
            if (
                container.name == DEPLOYMENT_NAME
                and container.resources
                and container.resources.limits
            ):
                container.resources.limits["memory"] = memory_limit


class UpdateService(Patch):
    """Update the Service object."""

    def __call__(self, obj):
        """Update the Service object in the deployment."""
        if not (obj.kind == "Service" and obj.metadata.name == SERVICE_NAME):
            return
        obj = cast(Service, obj)
        if obj.spec is None:
            return
        obj.spec.clusterIP = None


class CoreDNSManifests(Manifests):
    """Deployment Specific details for the coredns."""

    def __init__(self, charm):
        manipulations = [
            ManifestLabel(self),
            ConfigRegistry(self),
            UpdateConfigMap(self),
            UpdateDeployment(self),
            UpdateService(self),
        ]
        super().__init__("coredns", charm.model, "upstream/coredns", manipulations)
        self.purging = False
        self.charm = charm

    @property
    def config(self) -> Dict:
        """Returns current config available from charm config and joined relations."""
        config = {**self.charm.model.config}
        for key, value in dict(**self.charm.model.config).items():
            if value == "" or value is None:
                del config[key]
        config["release"] = config.pop("coredns_release", None)
        return config

    def hash(self) -> int:
        """Calculate a hash of the current configuration."""
        json_str = json.dumps(self.config, sort_keys=True)
        hash = hashlib.sha256()
        hash.update(json_str.encode())
        return int(hash.hexdigest(), 16)

    def evaluate(self) -> Optional[str]:
        """Determine if manifest_config can be applied to manifests."""
        props = ["corefile"]
        for prop in props:
            value = self.config.get(prop)
            if not value:
                return f"Provider manifests waiting for definition of {prop}"
        return None

    def get_service_address(self) -> str:
        """Get the ClusterIP address of the CoreDNS service."""
        try:
            service = self.client.get(ServiceRes, SERVICE_NAME, namespace=SERVICE_NS)
            if service and service.spec and service.spec.clusterIP:
                return service.spec.clusterIP
        except HTTPError as e:
            log.error(f"Error getting service address: {e}")
        return ""
