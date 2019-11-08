# CoreDNS

CoreDNS is a DNS  server. And this is a Kubernetes Charm to deploy CoreDNS on Kubernetes with Juju.

## How to build the charm

Make sure you've got the [coredns-interface](https://github.com/DomFleischmann/coredns-interface) in your $CHARM_INTERFACES_DIR and simply build the charm with:
``` charm build charm-coredns -o ..```

## How to deploy the charm

For deploying the charm you will have to have a kubernetes cluster bootstrapped with juju (more information [here](https://jaas.ai/docs/k8s-cloud) to be able to deploy kubernetes charms. Than you will simply need to execute the following in the charm builds dir:  
``` juju deploy ./coredns --resource coredns-image=rocks.canonical.com:443/cdk/coredns/coredns-amd64:1.6.2 ```

## How to add the relation
You can use the coredns interface to automatically configure the CoreDNS charm with Charmed Kubernetes. For that execute the following inside of the Charmed Kubernetes model:
```juju offer kubernetes-worker:coredns```
And the following in the model where CoreDNS has been deployed:
```juju add-relation coredns <offer-url>```
This will reconfigure the kubelet with the service IP of this CoreDNS.
