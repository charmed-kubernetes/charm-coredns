# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of coredns specific details of the kubernetes manifests."""

import hashlib
import json
import logging
from string import Template
from typing import Dict, Optional, Type, TypeVar

from httpx import HTTPError
from lightkube.core.resource import NamespacedResource
from lightkube.models.apps_v1 import Deployment
from lightkube.models.core_v1 import ConfigMap, Service, ServiceAccount
from lightkube.models.rbac_v1 import ClusterRole, ClusterRoleBinding
from lightkube.resources.core_v1 import Service as Service_Res
from ops.manifests import ConfigRegistry, ManifestLabel, Manifests, Patch
from ops.manifests.manipulations import Subtraction

log = logging.getLogger(__file__)
CLUSTER_ROLE_NAME = CLUSTER_ROLE_BINDING_NAME = "system:coredns"
SERVICE_ACCOUNT_NAME = DEPLOYMENT_NAME = "coredns"
SERVICE_NAME = "kube-dns"

T = TypeVar("T")


def _model_formatter(config_ns, model_name: str) -> str:
    """Format the namespace with the model name if applicable."""
    return config_ns.format(model=model_name)


def _matches(obj, kind: Type[T], name: str) -> Optional[T]:
    """Check if the object matches the given kind and name."""
    if isinstance(obj, kind) and obj.metadata and obj.metadata.name == name:
        return obj
    return None


class AdjustNamespace(Patch):
    """Adjust metadata namespace."""

    def __call__(self, obj) -> None:
        """Replace namespace if object supports it."""
        ns = _model_formatter(
            self.manifests.config["coredns_namespace"], self.manifests.model.name
        )
        if isinstance(obj, NamespacedResource) and obj.metadata:
            log.debug(f"Adjusting namespace for {obj.kind}/{obj.metadata.name} to {ns}")
            obj.metadata.namespace = ns
        if crb := _matches(obj, ClusterRoleBinding, CLUSTER_ROLE_BINDING_NAME):
            log.debug(
                f"Adjusting subjects namespace for {obj.kind}/{obj.metadata.name} to {ns}"
            )
            for subject in crb.subjects or []:
                subject.namespace = ns


class AdjustClusterRoleName(Patch):
    """Adjust RoleBinding name to be unique per model."""

    @property
    def name(self) -> str:
        """Generate a unique name for the ClusterRole based on the model."""
        model = self.manifests.model
        app = model.name
        uuid = model.uuid[:8]
        return f"juju:{app}-{uuid}:{model.app.name}"

    def __call__(self, obj) -> None:
        """Replace RoleBinding name."""
        name = self.name
        if crb := _matches(obj, ClusterRoleBinding, CLUSTER_ROLE_BINDING_NAME):
            crb.roleRef.name = name
            crb.metadata.name = name
        elif cr := _matches(obj, ClusterRole, CLUSTER_ROLE_NAME):
            cr.metadata.name = name


class AdjustConfigMap(Patch):
    """Update the ConfigMap object."""

    def __call__(self, obj):
        """Update the ConfigMap object in the manifests."""
        if not _matches(obj, ConfigMap, DEPLOYMENT_NAME):
            return
        assert obj.data
        obj.data["Corefile"] = self.corefile

    @property
    def corefile(self) -> str:
        """Return the rendered Corefile configuration."""
        corefile = Template(self.manifests.config["corefile"])
        return corefile.safe_substitute(self.manifests.config)


class AdjustDeployment(Patch):
    """Update the Deployment object."""

    def __call__(self, obj):
        """Update the Deployment object in the manifests."""
        if not _matches(obj, Deployment, DEPLOYMENT_NAME):
            return
        assert obj.spec and obj.spec.template.spec
        containers = obj.spec.template.spec.containers
        memory_limit = self.manifests.config.get("coredns_memory_limit", "170Mi")
        obj.spec.replicas = self.manifests.config.get("coredns_replicas", 1)
        obj.spec.template.spec.automountServiceAccountToken = True
        assert len(containers) == 1
        container = containers[0]
        assert container.resources.limits
        log.debug(
            f"Setting memory limit for container {container.name} to {memory_limit}"
        )
        container.resources.limits["memory"] = memory_limit


class AdjustService(Patch):
    """Update the Service object."""

    def __call__(self, obj):
        """Update the Service object in the deployment."""
        if not _matches(obj, Service, SERVICE_NAME):
            return
        assert obj.spec
        obj.spec.clusterIP = None


class AdjustServiceAccount(Subtraction):
    """Remove ServiceAccount from manifests."""

    def __call__(self, obj) -> bool:
        """Remove ServiceAccount from the manifests."""
        if not _matches(obj, ServiceAccount, SERVICE_ACCOUNT_NAME):
            return False
        ns = _model_formatter(
            self.manifests.config["coredns_namespace"], self.manifests.model.name
        )
        if ns == self.manifests.model.name:
            log.debug("Removing duplicate service account provided by juju")
        return ns == self.manifests.model.name


class CoreDNSManifests(Manifests):
    """Deployment Specific details for the coredns."""

    def __init__(self, charm):
        manipulations = [
            ManifestLabel(self),
            ConfigRegistry(self),
            AdjustNamespace(self),
            AdjustClusterRoleName(self),
            AdjustConfigMap(self),
            AdjustDeployment(self),
            AdjustService(self),
            AdjustServiceAccount(self),
        ]
        super().__init__("coredns", charm.model, "upstream/coredns", manipulations)
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

    def hash(self) -> str:
        """Calculate a hash of the current configuration."""
        json_str = json.dumps(self.config, sort_keys=True)
        hasher = hashlib.sha256()
        hasher.update(json_str.encode())
        return hasher.hexdigest()

    def evaluate(self) -> Optional[str]:
        """Determine if manifest_config can be applied to manifests."""
        props = ["corefile", "coredns_namespace"]
        for prop in props:
            value = self.config.get(prop)
            if not value:
                return f"Provider manifests waiting for definition of {prop}"
        return None

    def get_service_address(self) -> str:
        """Get the ClusterIP address of the CoreDNS service."""
        try:
            ns = _model_formatter(self.config["coredns_namespace"], self.model.name)
            service = self.client.get(Service_Res, SERVICE_NAME, namespace=ns)
            if service and service.spec and service.spec.clusterIP:
                return service.spec.clusterIP
        except HTTPError as e:
            log.error(f"Error getting service address: {e}")
        return ""
