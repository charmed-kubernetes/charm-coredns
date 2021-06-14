# Copyright 2021 Ubuntu
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

COREFILE_BASE = """.:53 {
    errors
    health {
      lameduck 5s
    }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
      fallthrough in-addr.arpa ip6.arpa
      pods insecure
    }
    prometheus :9153
    forward . 1.1.1.1
    cache 30
    loop
    reload
    loadbalance
}

"""

EXTRA_SERVER = """. {{
    log
}}
"""

COREFILE_EXTRA = f"""{COREFILE_BASE[0:-2]}
{EXTRA_SERVER}
"""
