# CoreDNS Operator

## Description

[CoreDNS][] is a flexible, plugin-based DNS server, and is the recommended
solution for providing DNS to Kubernetes services within the cluster.
This operator enables integration with [Charmed Kubernetes][] via a
cross-model relation and allows for more customization than provided by the
deployment of CoreDNS provided by default by Charmed Kubernetes.

More information on using this operator with Charmed Kubernetes can be found
[here](https://ubuntu.com/kubernetes/docs/cdk-addons#coredns), and bugs should
be filed [here](https://bugs.launchpad.net/charmed-kubernetes).

## Usage

Sourced from: https://github.com/coredns/deployment.git

CoreDNS has been the default DNS provider for Charmed Kubernetes clusters
since 1.14.

For additional control over CoreDNS, you can also deploy it into the cluster
using the CoreDNS Kubernetes operator charm. To do so, set the dns-provider
kubernetes-master configuration option to none and deploy the charm into a
Kubernetes model on your cluster. You’ll also need to cross-model relate it
to kubernetes-master:

juju config -m cluster-model kubernetes-master dns-provider=none
juju add-k8s k8s-cloud --controller mycontroller
juju add-model k8s-model k8s-cloud
juju deploy corey-coredns coredns
juju trust --scope=cluster coredns
juju offer coredns:dns-provider
juju consume -m cluster-model k8s-model.coredns
juju relate -m cluster-model coredns kubernetes-master

Once everything settles out, new or restarted pods will use the CoreDNS charm
as their DNS provider. The CoreDNS charm config allows you to change the
cluster domain, the IP address or config file to forward unhandled queries
to, add additional DNS servers, or even override the Corefile entirely.

## Developing

Get the charm operator code

    git clone --branch sidecar https://github.com/charmed-kubernetes/charm-coredns.git
    cd charm-coredns

Create and activate a virtualenv with the development requirements:

    virtualenv -p python3 venv
    source venv/bin/activate
    pip install -r requirements-dev.txt

Build the charm

    charmcraft build

## Testing

The Python operator framework includes a very nice harness for testing
operator behaviour without full deployment. Just `run_tests`:

    ./run_tests

[CoreDNS]: https://coredns.io/
[Charmed Kubernetes]: https://ubuntu.com/kubernetes/docs
