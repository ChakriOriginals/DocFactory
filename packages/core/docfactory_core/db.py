"""Engine, sessions, and the tenant context that row level security reads.

Sync SQLAlchemy everywhere: the worker is a plain process and FastAPI runs
`def` endpoints in its threadpool, so async DB would add complexity this
project does not need yet.

Tenant isolation works by setting `app.tenant_id` inside each transaction; the
RLS policies compare every row against it. Two properties matter:

*`SET LOCAL`, never `SET`.* The setting is scoped to the transaction and is
discarded on commit or rollback, so a connection returned to the pool cannot
carry one tenant's context into the next tenant's request. A plain `SET` would
persist for the life of the pooled connection — the exact leak this design
exists to prevent.

*Fail closed.* With no tenant bound, `current_setting('app.tenant_id', true)`
is NULL and every policy comparison is NULL, so a query returns nothing rather
than everything. Forgetting to bind a tenant loses data access, not privacy.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from docfactory_core.config import get_settings

# The tenant the current unit of work belongs to. Set by API auth per request
# and by the worker per message; read by session_scope when it opens a
# transaction. A ContextVar rather than a parameter so it cannot be forgotten
# halfway down a call stack.
current_tenant: ContextVar[str | None] = ContextVar("current_tenant", default=None)


class TenantContextError(RuntimeError):
    """A tenant-scoped operation ran without a tenant bound."""


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def tenant_context(tenant_id: str) -> Iterator[str]:
    """Bind a tenant for the enclosed block, restoring the previous one after."""
    token = current_tenant.set(tenant_id)
    try:
        yield tenant_id
    finally:
        current_tenant.reset(token)


@contextmanager
def session_scope(
    tenant_id: str | None = None, *, require_tenant: bool = True
) -> Iterator[Session]:
    """Commit on success, rollback on error, with the tenant bound for RLS.

    `require_tenant=False` is for the few genuinely cross-tenant paths — API
    key lookup during authentication, and admin tooling — which read tables
    that carry no tenant_id. It never widens access to tenant-scoped tables:
    those are governed by the policies, which see no tenant and return nothing.
    """
    tenant = tenant_id or current_tenant.get()
    if tenant is None and require_tenant:
        raise TenantContextError(
            "no tenant bound: wrap the call in tenant_context(...) or pass tenant_id"
        )

    session = get_sessionmaker()()
    try:
        if tenant is not None:
            # Parameterized: a tenant id reaches this from an API key lookup,
            # and string-formatting it into SQL would be an injection seam.
            session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant}
            )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def admin_session_scope() -> Iterator[Session]:
    """Owner connection, bypassing RLS. Migrations and tests only.

    Deliberately separate and deliberately awkward to reach: anything using
    this is outside the isolation guarantees the rest of the system relies on.
    """
    engine = create_engine(get_settings().database_admin_url)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()
