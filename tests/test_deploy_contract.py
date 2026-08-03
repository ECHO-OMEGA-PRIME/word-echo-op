from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_migration_contract_and_exact_asset() -> None:
    contract = json.loads((ROOT / "migration_contract.json").read_text(encoding="utf-8"))
    assert contract["worker_name"] == "word-echo-op"
    assert contract["handlers"] == {
        "fetch": True,
        "scheduled": False,
        "queue": False,
        "email": False,
    }
    assert contract["routes"]["manual_generic_contract_coverage"] == "2/2"
    assert contract["routes"]["source_non_generic_route_count"] == 0
    assert contract["bindings"]["d1"] is False
    assert contract["bindings"]["secrets"] is False
    asset = (ROOT / contract["static_asset"]["target_path"]).read_bytes()
    assert len(asset) == contract["static_asset"]["source_value_bytes"] == 401921
    assert hashlib.sha256(asset).hexdigest() == contract["static_asset"]["source_value_sha256"]


def test_deploy_gate_contains_required_release_proofs() -> None:
    deploy = (ROOT / "deploy_word_echo_op.sh").read_text(encoding="utf-8")
    for marker in (
        "provenance_verified",
        "staging_smoke",
        "production_candidate_active",
        "production_smoke",
        "rollback_smoke",
        "WORD_ECHO_FORCE_STAGING_SMOKE_FAIL",
        "WORD_ECHO_FORCE_PROD_SMOKE_FAIL",
        "BindReadOnlyPaths",
        'ln -sfn current/app.py "$BASE_DIR/app.py"',
    ):
        assert marker in deploy
    assert "DATABASE_URL" not in deploy
    assert "EnvironmentFile" not in (ROOT / "systemd" / "word-echo-op.service").read_text(encoding="utf-8")


def test_route_cutover_has_backup_and_rollback() -> None:
    route = (ROOT / "register_tunnel_route.py").read_text(encoding="utf-8")
    assert "route-backup.json" in route
    assert "def rollback" in route
    assert "dns_points_to_tunnel" in route
    assert "echo.vault.get" in route
    assert "/workers/domains" in route
    assert "read_only" in route
