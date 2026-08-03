#!/usr/bin/env bash
# Staging-first, immutable-release deployment for word-echo-op.
set -euo pipefail

SRC_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BASE_DIR=/home/forge/word-echo-op
RELEASES_DIR="$BASE_DIR/releases"
CURRENT_LINK="$BASE_DIR/current"
UNIT=word-echo-op.service
PROD_PORT=8464
STAGING_PORT=8465
RUN_USER=word-echo-op
RUNTIME_MOUNT=/opt/word-echo-op-runtime
STAGING_MOUNT=/opt/word-echo-op-staging
TEST_PYTHON="${WORD_ECHO_TEST_PYTHON:-/home/forge/echo-worker-server/venv/bin/python}"
STRICT_BUNDLE=/mnt/cf_kv_r2/workers/word-echo-op/source/index.js
STRICT_BINDINGS=/mnt/cf_kv_r2/workers/word-echo-op/bindings.json
STRICT_SETTINGS=/mnt/cf_kv_r2/workers/word-echo-op/settings.json
STRICT_MANIFEST=/mnt/cf_kv_r2/workers/word-echo-op/source/__STATIC_CONTENT_MANIFEST.js
STRICT_ASSET=/mnt/cf_kv_r2/kv_namespaces/__word-echo-op-workers_sites_assets/values/7438576bf2183a96.bin
CATALOG_SOURCE=/mnt/echo_ibm_1tb/_archive/cf_rescue_d1/workers/source/word-echo-op.js
RELEASE_ID="$(date -u +%Y%m%dT%H%M%S%NZ)-$(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || echo source)"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
STAGING_UNIT=""
OLD_TARGET=""
UNIT_BACKUP="$BASE_DIR/unit-backups/$RELEASE_ID/word-echo-op.service"

CATALOG_SHA=84d29f7b2fa2718801797eb237efa0920cd210baaeeb10104ef8cd9d3c22e437
STRICT_SHA=70d697061b7e5d2a0fe2642342d87ebed7046fb2c5de8bfbbd34ca3f70bc3d05
REPOSITORY_SHA=2a2ec38174de4277b199980d5ac7f2d379653308535fcbba70353ef151a0c5ee
ASSET_SHA=364263a47a7a44b12fadaf0f81b83d1a11813e46107e874523e4701468945a14
BINDINGS_SHA=f7dee6a5143bd68fa601e95551f595b8e9ff3aec16dc75e52ab4a231b14be98f
SETTINGS_SHA=55e23c9407b8535881e5fa3165b48e2147a13718b795fd6360396b6335582bcd
MANIFEST_SHA=63ffcbc086d7fdbb5b8471a47303686f90429c8eed5cd628aa79ba24ec384d7b

log() { printf '[word-echo-deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
cleanup() {
  if [ -n "$STAGING_UNIT" ]; then
    systemctl stop "$STAGING_UNIT.service" >/dev/null 2>&1 || true
    systemctl reset-failed "$STAGING_UNIT.service" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

wait_for_health() {
  local port="$1"
  for _ in $(seq 1 30); do
    curl -fsS --max-time 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

record_receipt() {
  local event_name="$1"
  local active_release="${2:-$RELEASE_DIR}"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -d echo \
    -v candidate_release="$RELEASE_DIR" \
    -v active_release="$active_release" \
    -v event_name="$event_name" >/dev/null <<SQL
INSERT INTO cf_word_echo_op.migration_receipts
    (candidate_release, active_release, event_name,
     catalog_rescue_sha256, strict_bundle_sha256,
     repository_source_sha256, static_asset_sha256,
     service_dir, unit_name, health_state)
VALUES (:'candidate_release', :'active_release', :'event_name',
        '$CATALOG_SHA', '$STRICT_SHA', '$REPOSITORY_SHA', '$ASSET_SHA',
        '$BASE_DIR', '$UNIT',
        CASE WHEN :'event_name' = 'provenance_verified' THEN 'verified' ELSE 'healthy' END)
ON CONFLICT (candidate_release, event_name) DO NOTHING;
SQL
}

backup_unit() {
  install -d -m 0755 "$(dirname "$UNIT_BACKUP")"
  if [ -f "/etc/systemd/system/$UNIT" ]; then
    cp -a "/etc/systemd/system/$UNIT" "$UNIT_BACKUP"
  else
    : > "$UNIT_BACKUP.absent"
  fi
}

restore_unit() {
  if [ -f "$UNIT_BACKUP.absent" ]; then
    rm -f "/etc/systemd/system/$UNIT"
  else
    install -m 0644 "$UNIT_BACKUP" "/etc/systemd/system/$UNIT"
  fi
}

rollback_release() {
  log "restoring prior known-green release"
  if [ -z "$OLD_TARGET" ]; then
    systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
    rm -f "$CURRENT_LINK"
    restore_unit
    systemctl daemon-reload
    return 0
  fi
  case "$OLD_TARGET" in "$RELEASES_DIR"/*) ;; *) return 1 ;; esac
  ln -s "releases/$(basename "$OLD_TARGET")" "$BASE_DIR/.rollback.$RELEASE_ID"
  mv -Tf "$BASE_DIR/.rollback.$RELEASE_ID" "$CURRENT_LINK"
  restore_unit
  systemctl daemon-reload
  systemctl restart "$UNIT"
  wait_for_health "$PROD_PORT"
  python3 "$CURRENT_LINK/smoke_live.py" --base "http://127.0.0.1:$PROD_PORT"
  record_receipt rollback_smoke "$OLD_TARGET"
}

if [ "$(id -u)" -ne 0 ]; then
  echo "deploy_word_echo_op.sh must run as root" >&2
  exit 2
fi
exec 9>/run/lock/word-echo-op-deploy.lock
flock -n 9 || { echo "another Word Echo deploy/finalization holds the release lock" >&2; exit 2; }
for required in app.py schema.sql smoke_live.py migration_contract.json systemd/word-echo-op.service public/index.html src/index.js; do
  [ -f "$SRC_DIR/$required" ] || { echo "invalid source directory: missing $required" >&2; exit 2; }
done
if [ ! -x "$TEST_PYTHON" ] || ! "$TEST_PYTHON" -c "import pytest" 2>/dev/null; then
  echo "verified pytest runner unavailable" >&2
  exit 2
fi
if ss -ltnH "sport = :$STAGING_PORT" | grep -q .; then
  echo "staging port $STAGING_PORT is occupied" >&2
  exit 2
fi
if [ ! -L "$CURRENT_LINK" ] && ss -ltnH "sport = :$PROD_PORT" | grep -q .; then
  echo "production port $PROD_PORT is occupied without a Word Echo release" >&2
  exit 2
fi

install -d -m 0755 "$BASE_DIR" "$RELEASES_DIR"
mkdir -m 0755 "$RELEASE_DIR"
rsync -a --exclude=.git --exclude=.pytest_cache --exclude=__pycache__ --exclude='*.pyc' \
  --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r "$SRC_DIR/" "$RELEASE_DIR/"
chmod 0755 "$RELEASE_DIR"

contract_value() {
  "$TEST_PYTHON" -c "import json; print(json.load(open('$RELEASE_DIR/migration_contract.json', encoding='utf-8'))$1)"
}
[ "$(contract_value "['provenance']['catalog_rescued_javascript_sha256']")" = "$CATALOG_SHA" ]
[ "$(contract_value "['provenance']['strict_recovered_bundle_sha256']")" = "$STRICT_SHA" ]
[ "$(contract_value "['provenance']['repository_worker_source_sha256']")" = "$REPOSITORY_SHA" ]
[ "$(contract_value "['static_asset']['source_value_sha256']")" = "$ASSET_SHA" ]
[ "$(contract_value "['static_asset']['source_value_count']")" = "1" ]
[ "$(contract_value "['static_asset']['source_value_bytes']")" = "401921" ]
[ "$(sha256sum "$RELEASE_DIR/src/index.js" | awk '{print $1}')" = "$REPOSITORY_SHA" ]
[ "$(sha256sum "$RELEASE_DIR/public/index.html" | awk '{print $1}')" = "$ASSET_SHA" ]
[ "$(stat -c %s "$RELEASE_DIR/public/index.html")" = "401921" ]
[ "$(sha256sum "$STRICT_BUNDLE" | awk '{print $1}')" = "$STRICT_SHA" ]
[ "$(sha256sum "$STRICT_BINDINGS" | awk '{print $1}')" = "$BINDINGS_SHA" ]
[ "$(sha256sum "$STRICT_SETTINGS" | awk '{print $1}')" = "$SETTINGS_SHA" ]
[ "$(sha256sum "$STRICT_MANIFEST" | awk '{print $1}')" = "$MANIFEST_SHA" ]
[ "$(sha256sum "$STRICT_ASSET" | awk '{print $1}')" = "$ASSET_SHA" ]
[ "$(stat -c %s "$STRICT_ASSET")" = "401921" ]
[ "$(sudo -H -u forge ssh -o BatchMode=yes -o ConnectTimeout=8 anvil "sha256sum '$CATALOG_SOURCE'" | awk '{print $1}')" = "$CATALOG_SHA" ]
[ "$(sudo -u postgres psql -d echo -Atc "SELECT btrim(source_sha256) FROM inventory.cf_migration_status WHERE lower(worker_name)='word-echo-op'")" = "$CATALOG_SHA" ]

"$TEST_PYTHON" -m py_compile "$RELEASE_DIR/app.py" "$RELEASE_DIR/smoke_live.py"
"$TEST_PYTHON" -m pytest -q --confcutdir="$RELEASE_DIR" "$RELEASE_DIR/tests"
python3 -m venv "$RELEASE_DIR/.venv"
PIP_CACHE_DIR="$BASE_DIR/pip-cache" \
  "$RELEASE_DIR/.venv/bin/python" -m pip install \
    --disable-pip-version-check --no-input --only-binary=:all: \
    --requirement "$RELEASE_DIR/requirements.txt" >/dev/null
"$RELEASE_DIR/.venv/bin/python" -c "import fastapi,uvicorn; assert fastapi.__version__ == '0.136.1'; assert uvicorn.__version__ == '0.46.0'"
systemd-analyze verify "$RELEASE_DIR/systemd/word-echo-op.service"

if ! getent passwd "$RUN_USER" >/dev/null; then
  useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin --user-group "$RUN_USER"
fi
[ "$(id -Gn "$RUN_USER")" = "$RUN_USER" ] || { echo "service identity has supplemental groups" >&2; exit 3; }
sudo -u postgres psql --single-transaction -v ON_ERROR_STOP=1 -d echo < "$RELEASE_DIR/schema.sql" >/dev/null
record_receipt provenance_verified
log "provenance, compile, tests, dependencies, identity, and unit verification GREEN"

STAGING_UNIT="word-echo-op-staging-$RELEASE_ID"
systemd-run --quiet --unit="$STAGING_UNIT" \
  --property="User=$RUN_USER" --property="Group=$RUN_USER" \
  --property="WorkingDirectory=$STAGING_MOUNT" \
  --property="BindReadOnlyPaths=$RELEASE_DIR:$STAGING_MOUNT" \
  --property=ProtectHome=tmpfs --property=ProtectSystem=strict \
  --property=PrivateTmp=yes --property=PrivateDevices=yes --property=NoNewPrivileges=yes \
  --setenv=WORD_ECHO_READS_PER_MINUTE=18 \
  --setenv=WORD_ECHO_PREFLIGHTS_PER_MINUTE=12 \
  --setenv="WORD_ECHO_ASSET_PATH=$STAGING_MOUNT/public/index.html" \
  /usr/bin/env "$STAGING_MOUNT/.venv/bin/python" -m uvicorn app:app \
    --host 127.0.0.1 --port "$STAGING_PORT" --workers 1 --log-level warning --no-access-log
wait_for_health "$STAGING_PORT" || { log "staging readiness RED; production untouched"; exit 4; }
staging_args=(--base "http://127.0.0.1:$STAGING_PORT" --exercise-rate-limit)
[ "${WORD_ECHO_FORCE_STAGING_SMOKE_FAIL:-0}" = "1" ] && staging_args+=(--force-fail)
python3 "$RELEASE_DIR/smoke_live.py" "${staging_args[@]}" || { log "staging smoke RED; production untouched"; exit 4; }
record_receipt staging_smoke
systemctl stop "$STAGING_UNIT.service"
systemctl reset-failed "$STAGING_UNIT.service" >/dev/null 2>&1 || true
STAGING_UNIT=""
log "staging smoke GREEN"

if [ -L "$CURRENT_LINK" ]; then OLD_TARGET="$(readlink -f "$CURRENT_LINK")"; fi
backup_unit
promote() {
  install -m 0644 "$RELEASE_DIR/systemd/word-echo-op.service" "/etc/systemd/system/$UNIT" || return 1
  ln -s "releases/$RELEASE_ID" "$BASE_DIR/.current.$RELEASE_ID" || return 1
  mv -Tf "$BASE_DIR/.current.$RELEASE_ID" "$CURRENT_LINK" || return 1
  systemctl daemon-reload || return 1
  systemctl enable "$UNIT" >/dev/null || return 1
  systemctl restart "$UNIT" || return 1
  wait_for_health "$PROD_PORT" || return 1
  record_receipt production_candidate_active "$RELEASE_DIR" || return 1
  prod_args=(--base "http://127.0.0.1:$PROD_PORT")
  [ "${WORD_ECHO_FORCE_PROD_SMOKE_FAIL:-0}" = "1" ] && prod_args+=(--force-fail)
  python3 "$CURRENT_LINK/smoke_live.py" "${prod_args[@]}" || return 1
  record_receipt production_smoke "$RELEASE_DIR" || return 1
}
if ! promote; then
  rollback_release || { log "promotion and rollback both RED"; exit 6; }
  log "promotion failed as requested/observed; rollback smoke GREEN"
  exit 5
fi
log "PROMOTED $RELEASE_ID; production smoke GREEN"
