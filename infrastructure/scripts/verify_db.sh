#!/usr/bin/env bash
# Verifies the database container is provisioned correctly.
set -euo pipefail

psql_owner() { docker compose exec -T db psql -U app_owner -d saas_ai -tAc "$1"; }

echo "== extensions =="
for ext in vector citext pgcrypto; do
  found=$(psql_owner "SELECT 1 FROM pg_extension WHERE extname = '$ext'")
  [ "$found" = "1" ] || { echo "FAIL: extension $ext missing"; exit 1; }
  echo "ok: $ext"
done

echo "== roles =="
for role in app_owner app_user; do
  found=$(psql_owner "SELECT 1 FROM pg_roles WHERE rolname = '$role'")
  [ "$found" = "1" ] || { echo "FAIL: role $role missing"; exit 1; }
  echo "ok: $role"
done

echo "== roles must NOT have BYPASSRLS =="
for role in app_owner app_user; do
  bypass=$(psql_owner "SELECT rolbypassrls FROM pg_roles WHERE rolname = '$role'")
  [ "$bypass" = "f" ] || { echo "FAIL: $role has BYPASSRLS"; exit 1; }
  echo "ok: $role is subject to RLS"
done

echo "== redis =="
docker compose exec -T redis redis-cli ping | grep -q PONG || { echo "FAIL: redis"; exit 1; }
echo "ok: redis"

echo "ALL CHECKS PASSED"
