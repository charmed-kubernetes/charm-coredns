# Copyright 2021 Ubuntu
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest
from unittest.mock import call, MagicMock

from charm import CharmCoreDNS
from ops.model import ActiveStatus, WaitingStatus
from ops.testing import Harness

from tests.unit import COREFILE_BASE, COREFILE_EXTRA, EXTRA_SERVER


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(CharmCoreDNS)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()
        container = self.harness.model.unit.get_container("coredns")
        container.push = MagicMock()
        container.stop = MagicMock()
        container.start = MagicMock()
        self.harness.model.get_binding = MagicMock()

    def test_coredns_pebble_ready(self):
        initial_plan = self.harness.get_container_pebble_plan("coredns")
        self.assertEqual(initial_plan.to_yaml(), "{}\n")
        expected_plan = {
            "services": {
                "coredns": {
                    "override": "replace",
                    "summary": "CoreDNS",
                    "command": "/coredns -conf /etc/coredns/Corefile",
                    "startup": "enabled",
                }
            },
        }
        container = self.harness.model.unit.get_container("coredns")
        self.harness.charm.on.coredns_pebble_ready.emit(container)
        updated_plan = self.harness.get_container_pebble_plan(
            "coredns").to_dict()
        self.assertEqual(expected_plan, updated_plan)
        service = self.harness.model.unit.get_container(
            "coredns").get_service("coredns")
        self.assertTrue(service.is_running())
        self.assertEqual(self.harness.model.unit.status,
                         WaitingStatus('Awaiting dns-provider relation'))

    def test_config_changed(self):
        self.harness.charm._is_running = MagicMock(return_value=True)
        self.harness.update_config({"forward": "1.1.1.1"})
        self.harness.update_config({"extra_servers": EXTRA_SERVER})
        container = self.harness.model.unit.get_container("coredns")
        container.push.assert_has_calls([
            call("/etc/coredns/Corefile", COREFILE_BASE, make_dirs=False),
            call("/etc/coredns/Corefile", COREFILE_EXTRA, make_dirs=False),
        ])

    def test_dns_provider_relation_changed(self):
        self.harness.charm._is_running = MagicMock(return_value=True)
        relation_id = self.harness.add_relation("dns-provider",
                                                "kubernetes-master")
        self.harness.add_relation_unit(relation_id, "kubernetes-master/0")
        self.harness.update_relation_data(relation_id, "kubernetes-master", {})
        self.assertEqual(self.harness.model.unit.status,
                         ActiveStatus('CoreDNS started'))
