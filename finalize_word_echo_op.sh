#!/usr/bin/env bash
set -euo pipefail

BASE_DIR=/home/forge/word-echo-op
CURRENT_LINK=$BASE_DIR/current
PROD_PORT=8464
UNIT=word-echo-op.service

[ "$(id -u)" -eq 0 ] || { echo "finalize_word_echo_op.sh must run as root" >&2; exit 2; }
exec 9>/run/lock/word-echo-op-deploy.lock
flock -n 9 || { echo "another Word Echo deploy/finalization holds the release lock" >&2; exit 2; }
ACTIVE_RELEASE="$(readlink -f "$CURRENT_LINK")"
case "$ACTIVE_RELEASE" in "$BASE_DIR"/releases/*) ;; *) echo "invalid active release" >&2; exit 3 ;; esac
[ -f "$ACTIVE_RELEASE/app.py" ] || { echo "active release missing" >&2; exit 3; }
systemctl is-active --quiet "$UNIT" || { echo "Word Echo unit is inactive" >&2; exit 3; }
python3 "$ACTIVE_RELEASE/smoke_live.py" --base "http://127.0.0.1:$PROD_PORT"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d echo \
  -v active_release="$ACTIVE_RELEASE" < "$ACTIVE_RELEASE/finalize_migration.sql" >/dev/null
echo "Word Echo active-release attestation and migration finalization are green"
