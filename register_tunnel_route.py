#!/usr/bin/env python3
"""Reversibly route word.echo-op.com through the existing private-cluster tunnel."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import time
import urllib.parse
import urllib.request
from pathlib import Path

ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "b9af3a4bf161132bb7e5d3d365fb8bb0")
TUNNEL_ID = os.environ.get("CF_TUNNEL_ID", "53f370a8-78c8-4146-8b57-f1577f85b327")
CF_API = os.environ.get("CF_API_BASE", "https://api.cloudflare.com/client/v4").rstrip("/")
CF_EMAIL = os.environ.get("CF_EMAIL", "bmcii1976@gmail.com")
HOSTNAME = "word.echo-op.com"
SERVICE = "http://127.0.0.1:8464"
ZONE_NAME = "echo-op.com"
TUNNEL_CNAME = f"{TUNNEL_ID}.cfargotunnel.com"
KEY_FILE = Path(os.environ.get("ECHO_SOVEREIGN_KEY_FILE", "/home/forge/.echo_sovereign_key"))
SDK_URL = os.environ.get("ECHO_SDK_URL", "http://127.0.0.1:8000/sdk/invoke")
BACKUP_PATH = Path(
    os.environ.get("WORD_ECHO_ROUTE_BACKUP", "/home/forge/word-echo-op/route-backup.json")
)


def sovereign_key() -> str:
    match = re.search(r"SOVEREIGN_KEY\s*=\s*(\S+)", KEY_FILE.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError("sovereign key unavailable")
    return match.group(1)


def cloudflare_key() -> str:
    payload = {
        "envelope_version": 1,
        "capability": "echo.vault.get",
        "params": {"command": "get", "service": "cloudflare_global_api_key"},
        "context": {
            "bypass_reason": (
                "Route the verified word.echo-op.com replacement through the existing "
                "Echo private-cluster tunnel with a recoverable configuration backup."
            )
        },
    }
    req = urllib.request.Request(
        SDK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Echo-API-Key": sovereign_key()},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read())
    secret = ((result.get("result") or {}).get("body") or {}).get("secret")
    if not secret:
        raise RuntimeError("Cloudflare credential unavailable")
    return str(secret)


def cf(method: str, path: str, key: str, payload: dict | None = None):
    req = urllib.request.Request(
        f"{CF_API}{path}",
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Auth-Email": CF_EMAIL,
            "X-Auth-Key": key,
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    if not raw:
        return {}
    result = json.loads(raw)
    if not result.get("success"):
        raise RuntimeError("Cloudflare operation failed")
    return result["result"]


def state(key: str) -> tuple[dict, str, list[dict], list[dict]]:
    tunnel = cf("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{TUNNEL_ID}/configurations", key)
    zones = cf("GET", f"/zones?name={urllib.parse.quote(ZONE_NAME)}", key)
    if len(zones) != 1:
        raise RuntimeError("expected one Cloudflare zone")
    zone_id = zones[0]["id"]
    records = cf(
        "GET", f"/zones/{zone_id}/dns_records?name={urllib.parse.quote(HOSTNAME)}", key
    )
    domains = cf(
        "GET", f"/accounts/{ACCOUNT_ID}/workers/domains?hostname={urllib.parse.quote(HOSTNAME)}", key
    )
    return tunnel, zone_id, records, domains


def mutable_dns(record: dict) -> dict:
    allowed = ("type", "name", "content", "ttl", "proxied", "comment", "tags", "settings")
    return {name: record[name] for name in allowed if name in record}


def save_backup(tunnel: dict, zone_id: str, records: list[dict], domains: list[dict]) -> None:
    if BACKUP_PATH.exists():
        raise RuntimeError(f"route backup already exists at {BACKUP_PATH}; inspect or rollback first")
    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hostname": HOSTNAME,
        "tunnel_config": tunnel.get("config") or {},
        "zone_id": zone_id,
        "dns_records": [mutable_dns(record) for record in records],
        "worker_domains": [
            {name: domain[name] for name in ("hostname", "service", "zone_id", "zone_name") if name in domain}
            for domain in domains
        ],
    }
    descriptor = os.open(BACKUP_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")


def ingress_with_word(config: dict) -> tuple[dict, int]:
    updated = dict(config)
    ingress = [rule for rule in list(config.get("ingress") or []) if rule.get("hostname") != HOSTNAME]
    index = len(ingress)
    for candidate, rule in enumerate(ingress):
        hostname = str(rule.get("hostname", ""))
        service = str(rule.get("service", ""))
        if hostname.startswith("*.") or not hostname or service.startswith("http_status"):
            index = candidate
            break
    ingress.insert(index, {"hostname": HOSTNAME, "service": SERVICE})
    updated["ingress"] = ingress
    return updated, index


def wait_for_dns(key: str, *, present: bool) -> list[dict]:
    for _ in range(30):
        _, _, records, _ = state(key)
        if bool(records) is present:
            return records
        time.sleep(1)
    raise RuntimeError("Cloudflare DNS transition did not converge")


def restore_backup(key: str, backup: dict) -> None:
    cf(
        "PUT",
        f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{TUNNEL_ID}/configurations",
        key,
        {"config": backup["tunnel_config"]},
    )
    _, zone_id, current_records, current_domains = state(key)
    originals = backup.get("dns_records") or []
    original_domains = backup.get("worker_domains") or []
    if len(current_records) > 1 or len(originals) > 1 or len(current_domains) > 1 or len(original_domains) > 1:
        raise RuntimeError("refusing ambiguous route rollback")
    if original_domains:
        if current_records:
            if (current_records[0].get("meta") or {}).get("read_only"):
                raise RuntimeError("unexpected managed DNS record blocks Worker-domain restore")
            cf("DELETE", f"/zones/{zone_id}/dns_records/{current_records[0]['id']}", key)
            wait_for_dns(key, present=False)
        if not current_domains:
            cf("PUT", f"/accounts/{ACCOUNT_ID}/workers/domains", key, original_domains[0])
            wait_for_dns(key, present=True)
        return
    if originals and current_records:
        cf("PUT", f"/zones/{zone_id}/dns_records/{current_records[0]['id']}", key, originals[0])
    elif originals:
        cf("POST", f"/zones/{zone_id}/dns_records", key, originals[0])
    elif current_records:
        cf("DELETE", f"/zones/{zone_id}/dns_records/{current_records[0]['id']}", key)


def apply(key: str) -> None:
    tunnel, zone_id, records, domains = state(key)
    if len(records) > 1 or len(domains) > 1:
        raise RuntimeError("refusing to mutate an ambiguous route state")
    if domains and domains[0].get("service") != "word-echo-op":
        raise RuntimeError("hostname is attached to an unexpected Worker")
    save_backup(tunnel, zone_id, records, domains)
    config, index = ingress_with_word(tunnel.get("config") or {})
    try:
        if domains:
            cf("DELETE", f"/accounts/{ACCOUNT_ID}/workers/domains/{domains[0]['id']}", key)
            records = wait_for_dns(key, present=False)
        cf(
            "PUT",
            f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{TUNNEL_ID}/configurations",
            key,
            {"config": config},
        )
        desired = {
            "type": "CNAME",
            "name": HOSTNAME,
            "content": TUNNEL_CNAME,
            "ttl": 1,
            "proxied": True,
        }
        if records:
            cf("PUT", f"/zones/{zone_id}/dns_records/{records[0]['id']}", key, desired)
        else:
            cf("POST", f"/zones/{zone_id}/dns_records", key, desired)
    except Exception:
        restore_backup(key, json.loads(BACKUP_PATH.read_text(encoding="utf-8")))
        raise
    print(json.dumps({"action": "applied", "hostname": HOSTNAME, "ingress_index": index}))


def rollback(key: str) -> None:
    backup = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
    if backup.get("hostname") != HOSTNAME:
        raise RuntimeError("route backup hostname mismatch")
    restore_backup(key, backup)
    print(json.dumps({"action": "rolled_back", "hostname": HOSTNAME}))


def status(key: str) -> None:
    tunnel, _, records, domains = state(key)
    ingress = list((tunnel.get("config") or {}).get("ingress") or [])
    matches = [rule for rule in ingress if rule.get("hostname") == HOSTNAME]
    print(
        json.dumps(
            {
                "dns_count": len(records),
                "dns_points_to_tunnel": len(records) == 1
                and records[0].get("content") == TUNNEL_CNAME,
                "hostname": HOSTNAME,
                "ingress_count": len(matches),
                "ingress_service_matches": len(matches) == 1 and matches[0].get("service") == SERVICE,
                "worker_domain_count": len(domains),
                "worker_domain_detached": len(domains) == 0,
            },
            sort_keys=True,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("apply", "rollback", "status"))
    args = parser.parse_args()
    key = cloudflare_key()
    {"apply": apply, "rollback": rollback, "status": status}[args.action](key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
