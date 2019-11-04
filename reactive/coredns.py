from pathlib import Path

from charmhelpers.core import hookenv
from charms.reactive import set_flag, clear_flag, endpoint_from_flag
from charms.reactive import when, when_not

from charms import layer


@when('layer.docker-resource.coredns-image.available')
@when_not('charm.coredns.started')
def start_charm():
    layer.status.maintenance('starting workload')

    # fetch the image info (registry path, auth info)
    image_info = layer.docker_resource.get_info('coredns-image')

    config = hookenv.config()

    corefile = Path('files/CoreFile').read_text() % config

    print('COREFILE: {}'.format(corefile))
    print('PORT: {}'.format(hookenv.config('port')))
    layer.caas_base.pod_spec_set({
        'serviceAccount': {
            'rules': [
                {
                    'apiGroups': [''],
                    'resources': ['endpoints', 'services', 'pods', 'namespaces'],
                    'verbs': ['list', 'watch'],
                },
                {
                    'apiGroups': [''],
                    'resources': ['nodes'],
                    'verbs': ['get'],
                },
            ]
        },
        'service': {
            'lables': {
                'k8s-app': 'kube-dns',
                'kubernetes.io/cluster-service': 'true',
                'kubernetes.io/name': "CoreDNS",
             },
        },
        'containers': [
            {
                'name': 'coredns-service',
                'imageDetails': {
                    'imagePath': image_info.registry_path,
                    'username': image_info.username,
                    'password': image_info.password,
                },
                'args': ["-conf", "/etc/coredns/CoreFile"],
                'ports': [
                    {'name': 'dns', 'containerPort': int(hookenv.config('port')), 'protocol': 'UDP'},
                    {'name': 'dns-tcp', 'containerPort': int(hookenv.config('port')), 'protocol': 'TCP'},
                    {'name': 'metrics', 'containerPort': 9153, 'protocol': 'TCP'},
                ],
                'readinessProbe': {
                    'httpGet': {'path': '/ready', 'port': 8181, 'scheme': 'HTTP'},
                },
                'files': [
                    {
                        'name': 'config',
                        'mountPath': '/etc/coredns',
                        'files': {
                            'CoreFile': corefile,
                        },
                    },
                ],
            },
        ],
    })

    layer.status.active('ready')
    set_flag('charm.coredns.started')


@when('layer.docker-resource.coredns-image.changed')
def update_image():
    # handle a new image resource becoming available
    clear_flag('charm.coredns.started')


@when('charm.coredns.started', 'coredns.joined')
def send_ip():
    """
    Send CoreDNS IP to kuberentes-worker
    """
    try:
        coredns = endpoint_from_flag('coredns.joined')
        if coredns:
            service_ip = get_service_ip('coredns')
            if service_ip:
                coredns.send_ip(service_ip)
                clear_flag('coredns.joined')
    except Exception as e:
        hookenv.log("Failed sending CoreDNS IP: {}".format(e))


def get_service_ip(endpoint):
    try:
        info = hookenv.network_get(endpoint, hookenv.relation_id())
        if 'ingress-addresses' in info:
            addr = info['ingress-addresses'][0]
            if len(addr):
                return addr
        else:
            hookenv.log("No ingress-addresses: {}".format(info))
    except Exception as e:
        hookenv.log("Caught exception checking for service IP: {}".format(e))

    return None
