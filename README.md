# CoreDNS Operator

[CoreDNS][] is a flexible, plugin-based DNS server, and is the recommended
solution for providing DNS to Kubernetes services within the cluster.
This operator enables integration with [Charmed Kubernetes][] via a
cross-model relation and allows for more customization than provided by the
deployment of CoreDNS provided by default by Charmed Kubernetes.

## Release selection

The default remains CoreDNS **v1.13.1** when `coredns_release` is unset. Newer
bundled releases are available only by explicitly configuring the release:

```bash
juju config coredns coredns_release=v1.14.2
```

Resetting `coredns_release` selects v1.13.1 again. Bundling a new release does not
change the default or automatically upgrade an existing deployment to it.

More information on using this operator with Charmed Kubernetes can be found
[here](https://ubuntu.com/kubernetes/docs/cdk-addons#coredns), and bugs should
be filed [here](https://bugs.launchpad.net/charmed-kubernetes).


[CoreDNS]: https://coredns.io/
[Charmed Kubernetes]: https://ubuntu.com/kubernetes/docs
