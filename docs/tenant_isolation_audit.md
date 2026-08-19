# Tenant isolation audit

Every table in the schema, classified, with the gaps named. Built by inspecting
the **live database** — `pg_class.relforcerowsecurity`, `pg_policies`,
`information_schema.role_table_grants` — not by reading what the migrations
were supposed to have done. That distinction is the whole point: the gap that
started this audit was a migration whose comment claimed something its SQL did
not do.

Audited 2026-08-18 against the compose Postgres at migration head
`089e1b235c11`. **All four gaps are closed** as of head `e5b2c1d8a3f7`; the
before-and-after matrices are both below, because the interesting part of this
document is the drift, not the end state.

---

## The matrix — BEFORE (head `089e1b235c11`)

`S/I/U/D` = SELECT / INSERT / UPDATE / DELETE granted to `docfactory_app`.

| table | `tenant_id`? | RLS enabled + FORCED | app grants | class | intended access | gap |
|---|:--:|:--:|:--:|---|---|---|
| `documents` | yes | ✅ / ✅ | S I U D | tenant data | own rows, read+write | — |
| `extractions` | yes | ✅ / ✅ | S I U D | tenant data | own rows, read+write | — |
| `extraction_fields` | yes | ✅ / ✅ | S I U D | tenant data | own rows, read+write | — |
| `review_tasks` | yes | ✅ / ✅ | S I U D | tenant data | own rows, read+write | — |
| `eval_cases` | yes | ✅ / ✅ | S I U D | tenant data | own rows, read+write | — |
| `usage_events` | yes | ✅ / ✅ | S I U D | tenant data | own rows, read+write | — |
| `tenant_spend` | yes | ✅ / ✅ | S I U D | tenant data | own rows, read+write | — |
| `tenant_rate_limits` | yes | ✅ / ✅ | S I U D | tenant data | own rows, read+write | — |
| **`pipelines`** | yes | ❌ / ❌ | S I U D | tenant data | own rows, read+write | **GAP 1** |
| **`api_keys`** | yes | ❌ / ❌ | **S I U D** | control plane | app **SELECT only** | **GAP 2** |
| `tenants` | no | ❌ / ❌ | S | control plane | app SELECT only | — |
| **`alembic_version`** | no | ❌ / ❌ | **S I U D** | migration bookkeeping | **none** | **GAP 3** |

Preconditions that make any of the ✅s mean anything, also verified live:

| property | value |
|---|---|
| `docfactory_app` `rolsuper` | `false` |
| `docfactory_app` `rolbypassrls` | `false` |
| `docfactory_app` CREATE on `public` | `false` |
| every table's owner | `docfactory` (hence FORCE matters — the owner would otherwise bypass its own policy) |

`drift_stats` does not exist. It is Phase 5 work and has not been built.

---

## What the audit found

### GAP 1 — `pipelines` has a `tenant_id` and no policy

A tenant-scoped table added in Phase 3b, after the Phase 3a RLS migration, with
full read/write grants to the application role and nothing confining them.
Reproduced as `docfactory_app`, bound to `dev-tenant`:

```
  documents (RLS'd)      : 0 foreign rows visible
  usage_events (RLS'd)   : 0 foreign rows visible
  pipelines (NO policy)  : 3 rows visible -> [('acme-tenant', 'secret_recipe', 'acme secret'),
                                              ('dev-tenant', 'invoice', 'invoice'),
                                              ('dev-tenant', 'purchase_order', 'purchase order')]
  *** app role DISABLED 1 of acme-tenant's pipelines — cross-tenant write confirmed
```

A pipeline definition is a tenant's extraction schema, its validation rules and
its model-routing policy — competitively meaningful configuration, and writable
by any tenant's process.

**Severity note, stated honestly:** no current code path reads this table.
`pipeline_registry.load_from_file()` loads definitions from `config/pipelines/*.json`,
so today the exposure is to a compromised task holding the app role's database
credentials, not to a tenant driving the HTTP API. But the registry's own
docstring says "a definition normally comes from the tenant's `pipelines` row",
and the day someone wires that read the hole becomes live with no other code
change and no test failure. Closing it now costs nothing precisely *because*
nothing reads it yet.

### GAP 2 — `api_keys` is writable by the application role

The hole surfaced in 4c.5 and deferred to this phase. Reproduced:

```
  api_keys (NO policy)   : 60 rows visible -> [('dev-tenant', ...), ('acme-tenant', 'dk_acme_test'), ...]
  *** app role MINTED an api key for acme-tenant — privilege escalation confirmed
```

This is the worst of the three, because it defeats isolation from *above*
rather than around: mint a key for another tenant, present it, and every RLS
policy in the matrix then works perfectly on behalf of the attacker. The row
policies are not bypassed — they are correctly applied to a tenant context that
was fraudulently obtained.

The Phase 3a migration's comment says both control-plane tables are "readable
by the app role but writable only by the owner". Only `tenants` was ever
revoked.

**Why the fix is a REVOKE and not a policy.** Authentication resolves a key
hash to a tenant *before* a tenant is known — that is what authentication is. A
tenant-scoped RLS policy on `api_keys` would compare against an unset
`app.tenant_id`, match nothing, and every login would fail. So the app keeps
unscoped `SELECT` and loses `INSERT`/`UPDATE`/`DELETE`. Key material is safe
under that read: rows store a SHA-256 hash and a 12-character prefix, never the
plaintext, so a full table read cannot be replayed as credentials.

### GAP 3 — `alembic_version` is writable by the application role

Not an isolation gap; an over-grant of the class trimmed from the IAM policies
in 4c.5. Alembic connects as the owner (`alembic/env.py` sets
`sqlalchemy.url` from `database_admin_url`), so the application role has no
reason to touch the table at all, and an app role that can `UPDATE
alembic_version` can convince the next deploy that a migration it never ran has
already been applied.

### GAP 4 — the drift mechanism itself

The reason 1–3 exist:

```sql
ALTER DEFAULT PRIVILEGES GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO docfactory_app;
-- pg_default_acl: {docfactory_app=arwd/docfactory}
```

Every table created by the owner from that migration onward is granted full
read/write to the application role **automatically**, while the RLS policy that
confines those grants has to be written **by hand**, in the migration, by
someone who remembered. `pipelines` (3b) and the four Phase 4 tables were all
added after that default was set; four of the five got their policy, one did
not, and nothing failed.

**The fix for GAP 4 is not a privilege change.** Revoking the default would
mean a hand-written `GRANT` in every future migration — the same "remember to
do the thing" failure mode, moved. The fix is to make forgetting fail loudly:
`tests/test_isolation.py` now derives its table list **from the live schema**
rather than from a hardcoded tuple, and asserts that any table with a
`tenant_id` column has RLS enabled, FORCEd, and a `tenant_isolation` policy. Add
a tenant-scoped table without a policy and the suite goes red before the branch
merges.

That test is the durable deliverable of this audit. The three REVOKEs and the
one policy are what the audit found; the guard is what stops the next one.

---

## The matrix — AFTER (head `e5b2c1d8a3f7`)

Re-read from the live schema after both migrations:

| table | `tenant_id`? | RLS enabled + FORCED | app grants | change |
|---|:--:|:--:|:--:|---|
| `documents` | yes | ✅ / ✅ | S I U D | — |
| `extractions` | yes | ✅ / ✅ | S I U D | — |
| `extraction_fields` | yes | ✅ / ✅ | S I U D | — |
| `review_tasks` | yes | ✅ / ✅ | S I U D | — |
| `eval_cases` | yes | ✅ / ✅ | S I U D | — |
| `usage_events` | yes | ✅ / ✅ | S I U D | — |
| `tenant_spend` | yes | ✅ / ✅ | S I U D | — |
| `tenant_rate_limits` | yes | ✅ / ✅ | S I U D | — |
| `pipelines` | yes | ✅ / ✅ | S I U D | **policy added** (`d4a1f0c9b7e2`) |
| `api_keys` | yes | — (exempt, documented) | **S** | **I/U/D revoked** (`e5b2c1d8a3f7`) |
| `tenants` | no | — | S | — |
| `alembic_version` | no | — | **(none)** | **all revoked** (`e5b2c1d8a3f7`) |

Behaviour, re-run as `docfactory_app` after the change:

```
  mint a key      -> InsufficientPrivilege: permission denied for table api_keys
  update keys     -> InsufficientPrivilege
  rewrite alembic -> InsufficientPrivilege
  pipelines, unscoped SELECT  -> own rows only
  pipelines, cross-tenant UPDATE -> 0 rows

  resolve_tenant('dev-local-key')     -> dev-tenant      (login still works)
  resolve_tenant('dk_acme_test_key')  -> acme-tenant
```

## What stops this happening again

Four guards, in decreasing order of how often they run:

| guard | where | runs |
|---|---|---|
| `test_every_tenant_scoped_table_is_protected` | `tests/test_isolation.py` | every CI push — reads the live schema and fails on any tenant-scoped table without a forced policy |
| `test_api_keys_grants_are_exactly_select` + 10 more | `tests/test_isolation.py::TestControlPlaneTables` | every CI push |
| `check_database_grants` | `infra/localstack/verify.py` | `make localstack-verify` |
| `scripts/isolation_check.sql` | psql, against a **deployed** database | by hand, from the deploy runbook |

The last one exists because CI's Postgres is a throwaway container and the
deployed database is Neon. A green CI proves the migrations are correct; only a
check against the real database proves the real database ran them.

The test is the important one, and it is deliberately not parametrized over a
list of names — it asserts a property of the *set* of tables. A list is the
thing that went stale for two phases.

---

## Control-plane access: the intended path

Creating a tenant and minting an API key are **owner operations**. Neither is
reachable through the application role, and there is no HTTP endpoint for
either — the API surface is documents, usage and review, and nothing else.

| operation | who | how |
|---|---|---|
| create a tenant | owner | `INSERT INTO tenants …` via `psql`, or `admin_session_scope()` |
| mint an API key | owner | `issue_api_key(tenant_id, name)` — connects via `DATABASE_ADMIN_URL` |
| revoke an API key | owner | `revoke_api_key(key_id)` — same |
| resolve a key at login | **app** | `resolve_tenant(presented)` — unscoped `SELECT` on `api_keys`, the one legitimate cross-tenant read |

**Confirmed end to end:** the API exposes no endpoint that touches `api_keys`
or `tenants` — the surface is `/documents`, `/usage`, `/review/*`, `/healthz`
and the internal storage-event bridge — and the `apps/` tree never imports
`issue_api_key`, `revoke_api_key` or `admin_session_scope` at all. The grant
was the only thing missing, not the path.

`issue_api_key` and `revoke_api_key` moved from `session_scope()` to
`admin_session_scope()` in this phase. That matters beyond tidiness: in a
deployed task `DATABASE_ADMIN_URL` is unset — only the one-off migrate task is
wired to the owner secret — so an API process that somehow reached these
functions fails to connect rather than succeeding quietly. The grant and the
code path now say the same thing, which is the property that was missing.

To mint a key against a deployed stack, from a machine that holds the owner
URL:

```bash
DATABASE_ADMIN_URL="$NEON_OWNER_URL" uv run python -c "
from docfactory_core.auth import issue_api_key
print(issue_api_key('dev-tenant', 'runbook').plaintext)"
```

The plaintext is printed once and stored only as a hash. Note the variable:
`DATABASE_URL` (the app role) would now fail with `permission denied`, which is
the intended and useful error.

---

## Flagged, not changed

- **`s3:GetBucketLocation`** on the IAM task roles has no call site and is kept
  deliberately (see the 4c.5 pre-flight table). Unrelated to the database, noted
  here so the "every grant has a call site" claim stays honest in one place.
- **`tenants` remains readable by the app role, unscoped.** `resolve_tenant`
  needs the row to check `status`, and budget/SLA lookups read it per request.
  A tenant-scoped policy would be correct in principle but the table holds
  nothing sensitive across tenants beyond a display name and a budget figure,
  and the auth path has the same before-the-tenant-is-known problem as
  `api_keys`. Left as read-only. Revisit if `tenants` ever gains a secret column.
- **`api_keys` stays unscoped for SELECT.** A compromised task can enumerate
  every tenant's key *hashes* and prefixes. That is the deliberate cost of
  hash-based authentication in one table; the hashes are not credentials.
