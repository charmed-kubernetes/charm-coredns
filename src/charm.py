#!/usr/bin/env python3

import logging
from string import Template

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus, WaitingStatus

from oci_image import OCIImageResource, OCIImageResourceError


class CoreDNSCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            self.model.unit.status = WaitingStatus('Waiting for leadership')
            return
        self.log = logging.getLogger(__name__)
        self.image = OCIImageResource(self, 'coredns-image')
        for event in [self.on.install,
                      self.on.leader_elected,
                      self.on.upgrade_charm,
                      self.on.config_changed]:
            self.framework.observe(event, self.main)
        self.framework.observe(self.on.dns_provider_relation_joined, self.provide_dns)

    def main(self, event):
        try:
            image_details = self.image.fetch()
        except OCIImageResourceError as e:
            self.model.unit.status = e.status
            return

        self.model.unit.status = MaintenanceStatus('Setting pod spec')

        corefile = Template(self.model.config['corefile'])
        corefile = corefile.safe_substitute(self.model.config)

        # Adapted from coredns.yaml.sed in https://github.com/coredns/ at 75a1cad
        self.model.pod.set_spec({
            'version': 3,
            'service': {
                'updateStrategy': {
                    'type': 'RollingUpdate',
                    'rollingUpdate': {'maxUnavailable': 1},
                },
                'annotations': {
                    'prometheus.io/port': "9153",
                    'prometheus.io/scrape': "true",
                },
            },
            # Dropped by a regression; see:
            # https://bugs.launchpad.net/juju/+bug/1895886
            # 'priorityClassName': 'system-cluster-critical',
            'containers': [{
                'name': 'coredns',
                'imageDetails': image_details,
                'imagePullPolicy': 'IfNotPresent',
                'args': ['-conf', '/etc/coredns/Corefile'],
                'volumeConfig': [{
                    'name': 'config-volume',
                    'mountPath': '/etc/coredns',
                    # Not supported
                    # 'readOnly': True,
                    'files': [{
                        'path': 'Corefile',
                        'mode': 0o444,
                        'content': corefile,
                    }],
                }],
                'ports': [
                    {
                        'name': 'dns',
                        'containerPort': 53,
                        'protocol': 'UDP',
                    },
                    {
                        'name': 'dns-tcp',
                        'containerPort': 53,
                        'protocol': 'TCP',
                    },
                    {
                        'name': 'metrics',
                        'containerPort': 9153,
                        'protocol': 'TCP',
                    },
                ],
                # Can't be specified by the charm yet; see:
                # https://bugs.launchpad.net/juju/+bug/1893123
                # 'resources': {
                #     'limits': {'memory': '170Mi'},
                #     'requests': {'cpu': '100m', 'memory': '70Mi'},
                # },
                'kubernetes': {
                    'securityContext': {
                        'allowPrivilegeEscalation': False,
                        'capabilities': {
                            'add': ['NET_BIND_SERVICE'],
                            'drop': ['all'],
                        },
                        'readOnlyRootFilesystem': True,
                    },
                    'livenessProbe': {
                        'httpGet': {
                            'path': '/health',
                            'port': 8080,
                            'scheme': 'HTTP',
                        },
                        'initialDelaySeconds': 60,
                        'timeoutSeconds': 5,
                        'successThreshold': 1,
                        'failureThreshold': 5,
                    },
                    'readinessProbe': {
                        'httpGet': {
                            'path': '/ready',
                            'port': 8181,
                            'scheme': 'HTTP',
                        },
                    },
                },
            }],
            'serviceAccount': {
                'roles': [{
                    'global': True,
                    'rules': [
                        {
                            'apigroups': ['discovery.k8s.io'],
                            'resources': [
                                'endpointslices',
                            ],
                            'verbs': ['list', 'watch'],
                        },
                        {
                            'apigroups': [''],
                            'resources': [
                                'endpoints',
                                'services',
                                'pods',
                                'namespaces',
                            ],
                            'verbs': ['list', 'watch'],
                        },
                        {
                            'apigroups': [''],
                            'resources': ['nodes'],
                            'verbs': ['get'],
                        },
                    ],
                }],
            },
            'kubernetesResources': {
                'pod': {
                    'dnsPolicy': 'Default',
                    # Not yet supported by Juju; see:
                    # https://bugs.launchpad.net/juju/+bug/1895887
                    # 'tolerations': [{
                    #     'key': 'CriticalAddonsOnly',
                    #     'operator': 'Exists',
                    # }],
                    # 'affinity': {
                    #      'podAntiAffinity': {
                    #          'preferredDuringScheduling' +
                    #          'IgnoredDuringExecution': [{
                    #               'weight': 100,
                    #               'podAffinityTerm': {
                    #                   'labelSelector': {
                    #                       'matchExpressions': [{
                    #                           'key': 'k8s-app',
                    #                           'operator': 'In',
                    #                           'values': ["kube-dns"],
                    #                       }],
                    #                   },
                    #                   'topologyKey': 'kubernetes.io/hostname',
                    #               },
                    #          }],
                    #      },
                    # },
                    # Can be done by the operator via placement (--to), but can't
                    # be specified by the charm yet, per same bug as above.
                    # 'nodeSelector': {
                    #     'kubernetes.io/os': 'linux',
                    # },
                }
            }
        })
        self.model.unit.status = ActiveStatus()

    def provide_dns(self, event):
        provided_data = event.relation.data[self.unit]
        if not provided_data.get('ingress-address'):
            event.defer()
            return
        provided_data.update({
            'domain': self.model.config['domain'],
            'sdn-ip': str(provided_data['ingress-address']),
            'port': "53",
        })


if __name__ == "__main__":
    main(CoreDNSCharm)
