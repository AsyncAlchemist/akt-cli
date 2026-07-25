#!/usr/bin/env bash
#
# Repeatable deploy of the akt-api Akaunting companion module to a server over
# SSH. Idempotent, self-verifying, and rolls back on any failure so a bad deploy
# never leaves the module half-installed (which can crash Akaunting's module
# tooling — see the alias note in akt-api/README.md).
#
# Usage:
#   AKT_EMAIL=… AKT_PASSWORD=… scripts/deploy-akt-api.sh
#
# Config (env vars):
#   AKT_API_SSH_HOST     (required)  ssh alias / host of the Akaunting server
#   AKT_API_APP_URL      (required)  Akaunting base URL, e.g. https://akaunting.example.com
#   AKT_API_REMOTE=akaunting         akaunting root, relative to the remote $HOME
#   AKT_API_COMPANY=1                company id to enable the module for
#   AKT_EMAIL / AKT_PASSWORD         for the authenticated 200 check (optional)
#
set -euo pipefail

SSH_HOST="${AKT_API_SSH_HOST:?set AKT_API_SSH_HOST to your ssh host/alias}"
APP_URL="${AKT_API_APP_URL:?set AKT_API_APP_URL to your Akaunting base URL}"
REMOTE="${AKT_API_REMOTE:-akaunting}"
COMPANY="${AKT_API_COMPANY:-1}"
ALIAS="akt-api"      # module.json alias (== the /api/<alias>/ URL prefix; Str::studly -> AktApi)
MODDIR="AktApi"      # install directory (StudlyCase, matches Str::studly(alias) + the namespace)
ENDPOINT="api/akt-api/ledgers"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_SRC="$(cd "$SCRIPT_DIR/../akt-api" && pwd)"

log()  { printf '==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; }
remote() { ssh "$SSH_HOST" "cd $REMOTE && $*"; }

rollback() {
    log "rolling back — removing the modules row + files, clearing caches"
    ssh "$SSH_HOST" "cd $REMOTE && \
        php artisan tinker --execute=\"DB::table('modules')->where('alias','$ALIAS')->delete();\" >/dev/null 2>&1 || true; \
        rm -rf modules/$MODDIR; \
        php artisan cache:clear >/dev/null 2>&1 || true; \
        php artisan route:clear >/dev/null 2>&1 || true" || true
}

[ -d "$MODULE_SRC" ] || { fail "module source not found: $MODULE_SRC"; exit 2; }

log "sync $MODULE_SRC/ -> $SSH_HOST:$REMOTE/modules/$MODDIR/"
rsync -az --delete "$MODULE_SRC"/ "$SSH_HOST:$REMOTE/modules/$MODDIR/"

# Enable = an upsert into the `modules` table (exactly what Akaunting's
# ModuleActivator::setActive does). We write the row directly instead of using
# `php artisan module:enable`, whose module-name resolution is unreliable for a
# hand-placed (non-marketplace) module. Idempotent: re-running just re-sets it.
log "enable '$ALIAS' for company $COMPANY (upsert the modules row)"
if ! remote "php artisan tinker --execute=\"DB::table('modules')->updateOrInsert(['company_id'=>$COMPANY,'alias'=>'$ALIAS','deleted_at'=>null],['enabled'=>1,'created_at'=>now(),'updated_at'=>now()]);\""; then
    fail "enable (modules row upsert) failed"
    rollback
    exit 1
fi

# Clear caches AFTER — refreshes the module status + route registration.
log "clear caches so the module status + routes refresh"
remote "php artisan cache:clear && php artisan config:clear && php artisan route:clear" >/dev/null

log "verify the route is registered"
if ! remote "php artisan route:list 2>/dev/null" | grep -q "$ENDPOINT"; then
    fail "route '$ENDPOINT' is not registered after enable"
    rollback
    exit 1
fi

log "verify app health (homepage must not be 5xx)"
home_code="$(curl -s -o /dev/null -w '%{http_code}' "$APP_URL/")"
case "$home_code" in
    5*) fail "homepage returned $home_code — the app may be broken"; rollback; exit 1 ;;
    *)  log "homepage HTTP $home_code (ok)" ;;
esac

if [ -n "${AKT_EMAIL:-}" ] && [ -n "${AKT_PASSWORD:-}" ]; then
    log "verify $ENDPOINT returns 200"
    ep_code="$(curl -s -o /dev/null -w '%{http_code}' -u "$AKT_EMAIL:$AKT_PASSWORD" \
        "$APP_URL/$ENDPOINT?company_id=$COMPANY&limit=1")"
    if [ "$ep_code" != "200" ]; then
        fail "endpoint returned $ep_code (want 200)"
        rollback
        exit 1
    fi
    log "endpoint HTTP 200 (ok)"
else
    log "AKT_EMAIL/AKT_PASSWORD not set — skipping the authenticated 200 check"
fi

log "deploy OK — '$ALIAS' enabled, /$ENDPOINT live on $APP_URL"
