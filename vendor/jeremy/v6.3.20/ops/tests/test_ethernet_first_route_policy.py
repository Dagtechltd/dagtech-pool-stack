from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate_network_route_policy.py"
SPEC = importlib.util.spec_from_file_location("validate_network_route_policy", SCRIPT)
route_policy = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = route_policy
SPEC.loader.exec_module(route_policy)


class EthernetFirstRoutePolicyTest(unittest.TestCase):
    def test_wifi_default_is_rejected_when_wired_route_exists(self) -> None:
        routes = route_policy.parse_default_routes(
            "\n".join(
                [
                    "default via 192.168.68.1 dev wlp0s20f3 proto dhcp src 192.168.68.56 metric 10",
                    "default via 192.168.1.1 dev enx207bd51aa286 proto static metric 600",
                ]
            )
        )
        route_get = route_policy.parse_route_get(
            "1.1.1.1 via 192.168.68.1 dev wlp0s20f3 src 192.168.68.56 uid 1000 cache"
        )

        issues = route_policy.route_policy_issues(routes, route_get)
        names = {issue.name for issue in issues}

        self.assertIn("wifi_default_preferred", names)
        self.assertIn("wifi_metric_not_subordinate", names)
        self.assertIn("egress_not_wired", names)

    def test_wired_default_with_wifi_fallback_is_accepted(self) -> None:
        routes = route_policy.parse_default_routes(
            "\n".join(
                [
                    "default via 192.168.1.1 dev enx207bd51aa286 proto static metric 10",
                    "default via 192.168.68.1 dev wlp0s20f3 proto dhcp src 192.168.68.56 metric 600",
                ]
            )
        )
        route_get = route_policy.parse_route_get(
            "1.1.1.1 via 192.168.1.1 dev enx207bd51aa286 src 192.168.1.120 uid 1000 cache"
        )

        self.assertEqual([], route_policy.route_policy_issues(routes, route_get))

    def test_dns_priority_drift_is_warned(self) -> None:
        connections = [
            route_policy.ActiveConnection(
                "Wired connection 1",
                "wired-uuid",
                "802-3-ethernet",
                "enx207bd51aa286",
                {
                    "connection.autoconnect-priority": "100",
                    "ipv4.route-metric": "10",
                    "ipv4.dns-priority": "0",
                },
            ),
            route_policy.ActiveConnection(
                "SkyMesh",
                "wifi-uuid",
                "802-11-wireless",
                "wlp0s20f3",
                {
                    "connection.autoconnect-priority": "0",
                    "ipv4.route-metric": "600",
                    "ipv4.dns-priority": "0",
                },
            ),
        ]

        issues = route_policy.nm_policy_issues(connections)
        names = {issue.name for issue in issues}

        self.assertIn("wired_dns_not_primary", names)
        self.assertIn("wifi_dns_priority_too_high", names)

    def test_apply_commands_keep_wifi_as_fallback_not_primary(self) -> None:
        connections = [
            route_policy.ActiveConnection("wired", "wired-uuid", "ethernet", "enp1s0"),
            route_policy.ActiveConnection("wifi", "wifi-uuid", "wifi", "wlp2s0"),
        ]

        commands = route_policy.build_apply_commands(connections)
        joined = [" ".join(command) for command in commands]

        self.assertTrue(any("wired-uuid" in command and "ipv4.route-metric 10" in command for command in joined))
        self.assertTrue(any("wired-uuid" in command and "ipv4.dns-priority -100" in command for command in joined))
        self.assertTrue(any("wifi-uuid" in command and "ipv4.route-metric 600" in command for command in joined))
        self.assertTrue(any("wifi-uuid" in command and "ipv4.dns-priority 600" in command for command in joined))
        self.assertTrue(any(command == "nmcli device reapply enp1s0" for command in joined))
        self.assertTrue(any(command == "nmcli device reapply wlp2s0" for command in joined))


if __name__ == "__main__":
    unittest.main()
