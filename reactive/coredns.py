from charmhelpers.core import hookenv
from charms.reactive import set_flag, clear_flag
from charms.reactive import when, when_not

from charms import layer


@when('layer.docker-resource.coredns-image.available')
@when_not('charm.coredns.started')
def start_charm():
    layer.status.maintenance('starting workload')

    # fetch the image info (registry path, auth info)
    image_info = layer.docker_resource.get_info('coredns-image')

    config = hookenv.config()
    port = config.get('port')

    layer.caas_base.pod_spec_set({
        'containers': [
            {
                'name': 'coredns-service',
                'imageDetails': {
                    'imagePath': image_info.registry_path,
                    'username': image_info.username,
                    'password': image_info.password,
                },
                'command': ['/coredns', '-dns.port={}'.format(port)],
                'ports': [
                    {
                        'name': 'dns',
                        'containerPort': 1053,
                    },
                ],
                'config': {
                    'K8S_MODEL': hookenv.model_name(),
                },
            },
        ],
    })

    layer.status.active('ready')
    set_flag('charm.coredns.started')


@when('layer.docker-resource.coredns-image.changed')
def update_image():
    # handle a new image resource becoming available
    clear_flag('charm.coredns.started')
