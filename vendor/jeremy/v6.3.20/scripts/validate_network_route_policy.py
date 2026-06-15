#!/usr/bin/env python3
"""Validate and optionally apply mining-host route priority policy.

The pool host may have both wired Ethernet and Wi-Fi active. The intended
policy is deterministic:

* wired Ethernet is the preferred default route for host and Docker egress;
* Wi-Fi remains available as a fallback, but must not outrank wired;
* DNS follows the same priority order as routing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any


WIRED_PREFIXES = ("en", "eth")
WIFI_PREFIXES = ("wl",)
VIRTUAL_PREFIXES = ("docker", "br-", "veth", "zt", "wg", "tun", "tap", "tailscale")
DEFAULT_WIRED_ROUTE_METRIC = 10
DEFAULT_WIFI_ROUTE_METRIC = 600
DEFAULT_WIRED_DNS_PRIORITY = -100
DEFAULT_WIFI_DNS_PRIORITY = 600


@dataclass(frozen=True)
class Route:
    family: str
    dev: str
    metric: int
    line: str
    gateway: str = ""
    src: str = ""


@dataclass(frozen=True)
class ActiveConnection:
    name: str
    uuid: str
    nm_type: str
    device: str
    settings: dict[str, str] = field(default_factory=dict)

    @property
    def device_class(self) -> str:
        return classify_device(self.device, self.nm_type)


@dataclass(frozen=True)
class Issue:
    severity: str
    name: str
    detail: str
    mitigation: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "name": self.name,
            "detail": self.detail,
            "mitigation": self.mitigation,
            "evidence": self.evidence,
        }


def run(command: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def classify_device(device: str, nm_type: str = "") -> str:
    name = (device or "").strip().lower()
    kind = (nm_type or "").strip().lower()
    if kind in {"ethernet", "802-3-ethernet"}:
        return "wired"
    if kind in {"wifi", "wireless", "802-11-wireless"}:
        return "wifi"
    if name.startswith(VIRTUAL_PREFIXES):
        return "virtual"
    if name.startswith(WIFI_PREFIXES):
        return "wifi"
    if name.startswith(WIRED_PREFIXES):
        return "wired"
    return "unknown"


def parse_default_routes(text: str, family: str = "ipv4") -> list[Route]:
    routes: list[Route] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("default"):
            continue
        parts = line.split()
        dev = ""
        gateway = ""
        metric = 0
        src = ""
        for index, token in enumerate(parts[:-1]):
            if token == "dev":
                dev = parts[index + 1]
            elif token == "via":
                gateway = parts[index + 1]
            elif token == "metric":
                metric = safe_int(parts[index + 1], 0) or 0
            elif token == "src":
                src = parts[index + 1]
        routes.append(Route(family=family, dev=dev, metric=metric, line=line, gateway=gateway, src=src))
    return routes


def parse_route_get(text: str, family: str = "ipv4") -> Route | None:
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not line:
        return None
    parts = line.split()
    dev = ""
    gateway = ""
    src = ""
    for index, token in enumerate(parts[:-1]):
        if token == "dev":
            dev = parts[index + 1]
        elif token == "via":
            gateway = parts[index + 1]
        elif token == "src":
            src = parts[index + 1]
    return Route(family=family, dev=dev, metric=0, line=line, gateway=gateway, src=src)


def parse_active_connections(text: str) -> list[ActiveConnection]:
    connections: list[ActiveConnection] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":", 3)
        if len(parts) != 4:
            continue
        name, uuid, nm_type, device = parts
        connections.append(ActiveConnection(name=name, uuid=uuid, nm_type=nm_type, device=device))
    return connections


def parse_nmcli_get_values(text: str, fields: list[str]) -> dict[str, str]:
    values = text.splitlines()
    return {field: values[index].strip() if index < len(values) else "" for index, field in enumerate(fields)}


def best_route(routes: list[Route]) -> Route | None:
    candidates = [route for route in routes if route.dev and classify_device(route.dev) != "virtual"]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.metric)[0]


def route_policy_issues(routes: list[Route], route_get: Route | None = None) -> list[Issue]:
    issues: list[Issue] = []
    usable_routes = [route for route in routes if route.dev and classify_device(route.dev) != "virtual"]
    wired_routes = [route for route in usable_routes if classify_device(route.dev) == "wired"]
    wifi_routes = [route for route in usable_routes if classify_device(route.dev) == "wifi"]
    selected = best_route(usable_routes)

    if not usable_routes:
        issues.append(
            Issue(
                "warn",
                "default_route_missing",
                "no usable default route was found",
                "Configure a wired default route before expecting Docker, peer discovery, or external RPC checks to work.",
            )
        )
        return issues

    if selected and classify_device(selected.dev) == "wifi" and wired_routes:
        issues.append(
            Issue(
                "fail",
                "wifi_default_preferred",
                f"default route prefers Wi-Fi {selected.dev} even though wired route(s) exist",
                "Set the wired route metric lower than Wi-Fi, for example wired=10 and Wi-Fi=600.",
                {"selected": selected.line, "wired_routes": [route.line for route in wired_routes]},
            )
        )

    if wired_routes and wifi_routes:
        best_wired = min(route.metric for route in wired_routes)
        best_wifi = min(route.metric for route in wifi_routes)
        if best_wifi <= best_wired:
            issues.append(
                Issue(
                    "fail",
                    "wifi_metric_not_subordinate",
                    f"best Wi-Fi route metric {best_wifi} is not greater than best wired metric {best_wired}",
                    "Raise Wi-Fi route metric so it remains fallback-only when Ethernet is available.",
                    {
                        "wired_routes": [route.line for route in wired_routes],
                        "wifi_routes": [route.line for route in wifi_routes],
                    },
                )
            )

    if route_get and route_get.dev and wired_routes and classify_device(route_get.dev) != "wired":
        issues.append(
            Issue(
                "fail",
                "egress_not_wired",
                f"kernel selected {route_get.dev} for external egress while wired route(s) exist",
                "Reapply NetworkManager connection metrics, then verify `ip route get 1.1.1.1` uses Ethernet.",
                {"route_get": route_get.line, "wired_routes": [route.line for route in wired_routes]},
            )
        )

    return issues


def nm_policy_issues(connections: list[ActiveConnection]) -> list[Issue]:
    issues: list[Issue] = []
    wired = [connection for connection in connections if connection.device_class == "wired"]
    wifi = [connection for connection in connections if connection.device_class == "wifi"]
    if not connections:
        issues.append(
            Issue(
                "warn",
                "networkmanager_connections_unavailable",
                "active NetworkManager connection details are unavailable",
                "Install/use NetworkManager or verify equivalent route and DNS priorities in host provisioning.",
            )
        )
        return issues

    for connection in wired:
        route_metric = safe_int(connection.settings.get("ipv4.route-metric"))
        dns_priority = safe_int(connection.settings.get("ipv4.dns-priority"))
        autoconnect_priority = safe_int(connection.settings.get("connection.autoconnect-priority"), 0)
        if route_metric is None or route_metric > 100:
            issues.append(
                Issue(
                    "warn",
                    "wired_route_metric_not_explicit",
                    f"wired connection {connection.name} route metric is {route_metric}",
                    "Set wired route metric to 10 so reboots and DHCP renewals keep Ethernet preferred.",
                    {"connection": connection.name, "device": connection.device, "metric": route_metric},
                )
            )
        if dns_priority is None or dns_priority >= 0:
            issues.append(
                Issue(
                    "warn",
                    "wired_dns_not_primary",
                    f"wired connection {connection.name} DNS priority is {dns_priority}",
                    "Set wired DNS priority to a negative value so active Wi-Fi cannot win DNS selection.",
                    {"connection": connection.name, "device": connection.device, "dns_priority": dns_priority},
                )
            )
        if autoconnect_priority < 50:
            issues.append(
                Issue(
                    "warn",
                    "wired_autoconnect_priority_low",
                    f"wired connection {connection.name} autoconnect priority is {autoconnect_priority}",
                    "Set wired autoconnect priority above Wi-Fi so NetworkManager brings it up first.",
                    {
                        "connection": connection.name,
                        "device": connection.device,
                        "autoconnect_priority": autoconnect_priority,
                    },
                )
            )

    for connection in wifi:
        route_metric = safe_int(connection.settings.get("ipv4.route-metric"))
        dns_priority = safe_int(connection.settings.get("ipv4.dns-priority"))
        if route_metric is not None and route_metric < 500:
            issues.append(
                Issue(
                    "warn",
                    "wifi_route_metric_too_low",
                    f"Wi-Fi connection {connection.name} route metric is {route_metric}",
                    "Set Wi-Fi route metric to 600 so it remains a fallback route.",
                    {"connection": connection.name, "device": connection.device, "metric": route_metric},
                )
            )
        if dns_priority is not None and dns_priority < 100:
            issues.append(
                Issue(
                    "warn",
                    "wifi_dns_priority_too_high",
                    f"Wi-Fi connection {connection.name} DNS priority is {dns_priority}",
                    "Set Wi-Fi DNS priority to 600 so wired DNS remains preferred.",
                    {"connection": connection.name, "device": connection.device, "dns_priority": dns_priority},
                )
            )

    if wired and wifi:
        best_wired_metric = min(
            metric for metric in (safe_int(item.settings.get("ipv4.route-metric")) for item in wired) if metric is not None
        ) if any(safe_int(item.settings.get("ipv4.route-metric")) is not None for item in wired) else None
        best_wifi_metric = min(
            metric for metric in (safe_int(item.settings.get("ipv4.route-metric")) for item in wifi) if metric is not None
        ) if any(safe_int(item.settings.get("ipv4.route-metric")) is not None for item in wifi) else None
        if best_wired_metric is not None and best_wifi_metric is not None and best_wifi_metric <= best_wired_metric:
            issues.append(
                Issue(
                    "fail",
                    "networkmanager_wifi_metric_not_subordinate",
                    f"NetworkManager Wi-Fi metric {best_wifi_metric} is not greater than wired metric {best_wired_metric}",
                    "Persist wired route metric lower than Wi-Fi in NetworkManager profiles.",
                    {
                        "wired": [item.name for item in wired],
                        "wifi": [item.name for item in wifi],
                    },
                )
            )

    return issues


def attach_settings(connections: list[ActiveConnection], settings_by_uuid: dict[str, dict[str, str]]) -> list[ActiveConnection]:
    return [
        ActiveConnection(
            name=connection.name,
            uuid=connection.uuid,
            nm_type=connection.nm_type,
            device=connection.device,
            settings=settings_by_uuid.get(connection.uuid, {}),
        )
        for connection in connections
    ]


def collect_active_connections() -> tuple[list[ActiveConnection], list[str]]:
    if not shutil.which("nmcli"):
        return [], ["nmcli unavailable"]
    proc = run(["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show", "--active"], timeout=5)
    if proc.returncode != 0:
        return [], [proc.stderr.strip() or "nmcli active connection query failed"]
    connections = parse_active_connections(proc.stdout)
    fields = [
        "connection.autoconnect-priority",
        "ipv4.route-metric",
        "ipv4.dns-priority",
        "ipv4.never-default",
        "ipv6.route-metric",
        "ipv6.dns-priority",
        "ipv6.never-default",
    ]
    settings_by_uuid: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for connection in connections:
        settings_proc = run(["nmcli", "-g", ",".join(fields), "connection", "show", connection.uuid], timeout=5)
        if settings_proc.returncode == 0:
            settings_by_uuid[connection.uuid] = parse_nmcli_get_values(settings_proc.stdout, fields)
        else:
            errors.append(settings_proc.stderr.strip() or f"nmcli settings query failed for {connection.name}")
    return attach_settings(connections, settings_by_uuid), errors


def collect_payload() -> dict[str, Any]:
    route4_proc = run(["ip", "-o", "-4", "route", "show", "default"], timeout=3)
    route_get_proc = run(["ip", "-o", "-4", "route", "get", "1.1.1.1"], timeout=3)
    routes4 = parse_default_routes(route4_proc.stdout, "ipv4") if route4_proc.returncode == 0 else []
    route_get = parse_route_get(route_get_proc.stdout, "ipv4") if route_get_proc.returncode == 0 else None
    connections, nm_errors = collect_active_connections()
    issues = route_policy_issues(routes4, route_get) + nm_policy_issues(connections)
    failures = [issue for issue in issues if issue.severity == "fail"]
    warnings = [issue for issue in issues if issue.severity == "warn"]
    selected = best_route(routes4)
    return {
        "ok": not failures,
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "selected_default_route": selected.line if selected else "",
        "route_get": route_get.line if route_get else "",
        "default_routes": [route.__dict__ for route in routes4],
        "active_connections": [
            {
                "name": connection.name,
                "uuid": connection.uuid,
                "type": connection.nm_type,
                "device": connection.device,
                "device_class": connection.device_class,
                "settings": connection.settings,
            }
            for connection in connections
        ],
        "nmcli_errors": nm_errors,
        "issues": [issue.as_dict() for issue in issues],
    }


def build_apply_commands(
    connections: list[ActiveConnection],
    *,
    wired_metric: int = DEFAULT_WIRED_ROUTE_METRIC,
    wifi_metric: int = DEFAULT_WIFI_ROUTE_METRIC,
    wired_dns_priority: int = DEFAULT_WIRED_DNS_PRIORITY,
    wifi_dns_priority: int = DEFAULT_WIFI_DNS_PRIORITY,
) -> list[list[str]]:
    commands: list[list[str]] = []
    for connection in connections:
        if connection.device_class == "wired":
            commands.append(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    connection.uuid,
                    "connection.autoconnect",
                    "yes",
                    "connection.autoconnect-priority",
                    "100",
                    "ipv4.never-default",
                    "no",
                    "ipv4.route-metric",
                    str(wired_metric),
                    "ipv4.dns-priority",
                    str(wired_dns_priority),
                    "ipv6.route-metric",
                    str(wired_metric),
                    "ipv6.dns-priority",
                    str(wired_dns_priority),
                ]
            )
        elif connection.device_class == "wifi":
            commands.append(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    connection.uuid,
                    "connection.autoconnect",
                    "yes",
                    "connection.autoconnect-priority",
                    "0",
                    "ipv4.never-default",
                    "no",
                    "ipv4.route-metric",
                    str(wifi_metric),
                    "ipv4.dns-priority",
                    str(wifi_dns_priority),
                    "ipv6.route-metric",
                    str(wifi_metric),
                    "ipv6.dns-priority",
                    str(wifi_dns_priority),
                ]
            )
    for connection in connections:
        if connection.device_class in {"wired", "wifi"} and connection.device:
            commands.append(["nmcli", "device", "reapply", connection.device])
    return commands


def apply_policy() -> list[dict[str, Any]]:
    connections, errors = collect_active_connections()
    results: list[dict[str, Any]] = [{"command": "collect_active_connections", "ok": not errors, "errors": errors}]
    if not shutil.which("nmcli"):
        return results
    for command in build_apply_commands(connections):
        proc = run(command, timeout=15)
        results.append(
            {
                "command": command,
                "ok": proc.returncode == 0,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        )
    return results


def print_human(payload: dict[str, Any]) -> None:
    if payload.get("selected_default_route"):
        print(f"DEFAULT {payload['selected_default_route']}")
    if payload.get("route_get"):
        print(f"EGRESS {payload['route_get']}")
    for issue in payload.get("issues", []):
        print(f"{issue['severity'].upper()} {issue['name']}: {issue['detail']}")
        if issue.get("mitigation"):
            print(f"  mitigation: {issue['mitigation']}")
    print(
        "SUMMARY "
        f"ok={payload['ok']} failures={payload['failure_count']} warnings={payload['warning_count']}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate BlockDAG mining host route priority policy.")
    parser.add_argument("--apply", action="store_true", help="Apply wired-first NetworkManager metrics before validation.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--warn-only", action="store_true", help="Always exit 0 after reporting issues.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply_results: list[dict[str, Any]] = []
    if args.apply:
        apply_results = apply_policy()
    payload = collect_payload()
    if apply_results:
        payload["apply_results"] = apply_results
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_human(payload)
    if args.warn_only:
        return 0
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
