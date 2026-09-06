#!/usr/bin/env bash
# Pre-flight for the Neon database, before any AWS spend.
#
# Run it in the shell where you exported the two connection strings:
#
#   ./scripts/verify_neon.sh
#
# It prints no secrets. Hosts and roles are shown; passwords never are, so the
# output is safe to paste into a chat, an issue, or a screenshot.
#
# WHAT IT IS ACTUALLY CHECKING. The Phase 3 isolation guarantee rests entirely
# on the application connecting as a role that cannot bypass row level
# security. A superuser ignores RLS completely — even with FORCE ROW LEVEL
# SECURITY — so a typo in CREATE ROLE would leave every policy in the schema
# as decoration while the isolation suite went on passing. That failure is
# invisible from inside the application, which is why it is checked here,
# against the real database, before anything is deployed on top of it.

set -uo pipefail

FAIL=0
pass() { printf "  \033[32mok\033[0m    %s\n" "$1"; }
fail() { printf "  \033[31mFAIL\033[0m  %s\n" "$1"; FAIL=1; }
info() { printf "        %s\n" "$1"; }

command -v psql >/dev/null 2>&1 || {
  echo "psql not found. brew install libpq, then add it to PATH:"
  echo '  export PATH="/opt/homebrew/opt/libpq/bin:$PATH"'
  exit 2
}

for var in TF_VAR_neon_database_url_owner TF_VAR_neon_database_url_app; do
  if [ -z "${!var:-}" ]; then
    echo "$var is not set. Export both connection strings first (runbook step 1.3)."
    exit 2
  fi
done

# SQLAlchemy wants postgresql+psycopg://; psql does not understand the driver
# suffix. Strip it for the checks, and never echo the result.
OWNER_URL="${TF_VAR_neon_database_url_owner/postgresql+psycopg:/postgresql:}"
APP_URL="${TF_VAR_neon_database_url_app/postgresql+psycopg:/postgresql:}"

# Host and database only — everything before the @ is a credential.
describe() { printf '%s' "$1" | sed -E 's|^[a-z+]+://[^@]*@||; s|\?.*$||'; }
role_of()  { printf '%s' "$1" | sed -E 's|^[a-z+]+://([^:]+):.*|\1|'; }

echo
echo "Neon pre-flight"
echo "  owner : $(role_of "$OWNER_URL") @ $(describe "$OWNER_URL")"
echo "  app   : $(role_of "$APP_URL") @ $(describe "$APP_URL")"
echo

q() { psql "$1" -tAX -c "$2" 2>&1; }

# --- 1. both connect ---------------------------------------------------------
if version=$(q "$OWNER_URL" "SHOW server_version"); then
  pass "owner connects (Postgres ${version%% *})"
  case "$version" in
    16*) pass "server is Postgres 16, matching compose and CI" ;;
    *)   fail "server is ${version%% *}; compose and CI run 16 — see docs/preflight_report.md" ;;
  esac
else
  fail "owner cannot connect: $version"
  echo; echo "Nothing else can be checked. Fix the owner URL first."; exit 1
fi

if q "$APP_URL" "SELECT 1" >/dev/null 2>&1; then
  pass "app role connects"
else
  fail "app role cannot connect: $(q "$APP_URL" 'SELECT 1' | head -1)"
  echo
  echo "  Nothing below can be checked without an app connection."
  echo "  Most likely the role has not been created yet — see runbook step 1.2."
  exit 1
fi

# --- 2. same database --------------------------------------------------------
[ "$(q "$OWNER_URL" 'SELECT current_database()')" = "$(q "$APP_URL" 'SELECT current_database()')" ] \
  && pass "both point at the same database" \
  || fail "the two URLs point at DIFFERENT databases — migrations would land somewhere the app cannot see"

# --- 3. the attributes the whole isolation model depends on -----------------
APP_ROLE=$(q "$APP_URL" "SELECT current_user")
attrs=$(q "$OWNER_URL" "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
                        FROM pg_roles WHERE rolname = '$APP_ROLE'")
if [ -z "$attrs" ]; then
  fail "role '$APP_ROLE' not found by the owner connection"
else
  IFS='|' read -r super bypass createdb createrole <<< "$attrs"
  [ "$super" = "f" ]      && pass "$APP_ROLE is NOT a superuser"        || fail "$APP_ROLE IS A SUPERUSER — it bypasses every RLS policy"
  [ "$bypass" = "f" ]     && pass "$APP_ROLE does NOT have BYPASSRLS"   || fail "$APP_ROLE HAS BYPASSRLS — every isolation policy is decoration"
  [ "$createdb" = "f" ]   && pass "$APP_ROLE cannot create databases"   || fail "$APP_ROLE has CREATEDB"
  [ "$createrole" = "f" ] && pass "$APP_ROLE cannot create roles"       || fail "$APP_ROLE has CREATEROLE — it could mint itself a superuser"
fi

# --- 4. DDL is genuinely refused, not merely un-granted on paper ------------
ddl=$(q "$APP_URL" "CREATE TABLE _docfactory_preflight_should_fail (x int)")
if printf '%s' "$ddl" | grep -qi "permission denied\|must be owner"; then
  pass "app role is refused DDL (CREATE TABLE denied)"
else
  fail "app role CREATED A TABLE — it holds DDL rights it must not have"
  q "$OWNER_URL" "DROP TABLE IF EXISTS _docfactory_preflight_should_fail" >/dev/null
fi

# --- 5. the server refuses unencrypted connections ---------------------------
#
# NOT pg_stat_ssl, which is what this originally checked and which is wrong on
# Neon. Neon puts a proxy in front of the compute: your client's TLS session
# terminates at the proxy, and the proxy talks to Postgres over an internal
# connection that is not itself TLS. So pg_stat_ssl reports ssl=f on a
# perfectly encrypted client connection, and the check failed against a
# correctly configured database.
#
# The question that actually matters is not "did this connection negotiate
# TLS" but "would the server accept one that did not". So ask it directly: try
# to connect with encryption disabled and require that it be refused. That is
# true of Neon, false of the compose Postgres (which has no TLS at all and
# reports this honestly), and does not care what proxies sit in between.
PLAIN_URL="${APP_URL%%\?*}?sslmode=disable"
if psql "$PLAIN_URL" -tAX -c "SELECT 1" >/dev/null 2>&1; then
  fail "server ACCEPTS unencrypted connections - credentials would cross the network in clear"
  info "expected on the local compose Postgres; not acceptable for a deployed database"
else
  pass "server refuses unencrypted connections (TLS is enforced)"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "  All checks passed. Neon is ready; proceed to the data-plane apply."
else
  echo "  Something above failed. Do NOT apply until it is fixed —"
  echo "  a superuser app role would make the whole isolation story untrue."
fi
exit "$FAIL"
