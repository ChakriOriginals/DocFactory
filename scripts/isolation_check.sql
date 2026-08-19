-- Tenant-isolation spot check against a deployed database.
--
-- Run as the OWNER. Prints one row per problem and nothing when the schema is
-- correct, so "no rows" is the pass condition:
--
--     psql "$NEON_OWNER_URL" -f scripts/isolation_check.sql
--
-- The authoritative version of these checks is tests/test_isolation.py, which
-- runs in CI on every push. This exists because the deployed database is Neon
-- and CI's is a throwaway Postgres container: the two can drift, and the whole
-- isolation claim is about the one customers' documents are actually in.
--
-- See docs/tenant_isolation_audit.md for what each rule is protecting.

\pset tuples_only off

-- 1. Any table with a tenant_id must have RLS enabled, FORCEd, and a policy.
--    api_keys is the documented exception: authentication resolves a key hash
--    to a tenant before a tenant is bound, so a tenant-scoped policy there
--    would compare against an unset app.tenant_id and fail every login. It is
--    protected by grant instead, which rule 2 checks.
SELECT 'UNPROTECTED TENANT TABLE' AS problem,
       c.relname AS detail
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN information_schema.columns col
  ON col.table_name = c.relname AND col.table_schema = n.nspname
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND col.column_name = 'tenant_id'
  AND c.relname <> 'api_keys'
  AND NOT (c.relrowsecurity
           AND c.relforcerowsecurity
           AND EXISTS (SELECT 1 FROM pg_policies p WHERE p.tablename = c.relname))

UNION ALL

-- 2. The control plane is read-only to the application role. A write grant on
--    api_keys is a privilege escalation: mint a key for any tenant, present
--    it, and every row policy then works correctly on the attacker's behalf.
SELECT 'APP ROLE CAN WRITE THE CONTROL PLANE',
       g.table_name || ': ' || g.privilege_type
FROM information_schema.role_table_grants g
WHERE g.table_schema = 'public'
  AND g.grantee = 'docfactory_app'
  AND g.table_name IN ('api_keys', 'tenants', 'alembic_version')
  AND g.privilege_type <> 'SELECT'

UNION ALL

-- 2b. alembic_version should not be readable either — nothing in the
--     application touches migration bookkeeping.
SELECT 'APP ROLE CAN READ MIGRATION STATE',
       g.privilege_type
FROM information_schema.role_table_grants g
WHERE g.table_schema = 'public'
  AND g.grantee = 'docfactory_app'
  AND g.table_name = 'alembic_version'

UNION ALL

-- 3. The role attributes that make every policy above real. A superuser or a
--    BYPASSRLS role ignores policies entirely, which is the failure mode that
--    makes an isolation suite pass while isolating nothing.
SELECT 'APP ROLE BYPASSES RLS',
       'rolsuper=' || r.rolsuper || ' rolbypassrls=' || r.rolbypassrls
FROM pg_roles r
WHERE r.rolname = 'docfactory_app'
  AND (r.rolsuper OR r.rolbypassrls)

UNION ALL

-- 4. The app role must not be able to create objects in the schema.
SELECT 'APP ROLE CAN CREATE OBJECTS', 'CREATE on schema public'
WHERE has_schema_privilege('docfactory_app', 'public', 'CREATE');
