"""API keys and tenant resolution.

Keys are stored only as SHA-256 hashes. The plaintext is returned once, at
issue time, and is unrecoverable afterwards — a dump of `api_keys` cannot be
replayed as credentials. Lookup hashes the presented key and matches on the
hash, so the comparison is against a value the database never held in the
clear.

`api_keys` and `tenants` deliberately carry no RLS policy: authentication has
to read them *before* any tenant context exists. They are readable by the
application role and writable only by the owner, so a compromised application
role cannot mint itself a key.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from docfactory_core.db import session_scope
from docfactory_core.models import ApiKey, Tenant, TenantStatus

log = logging.getLogger(__name__)

_KEY_PREFIX = "dk_"
_KEY_BYTES = 24


class AuthError(Exception):
    """The presented credential is missing, unknown, or revoked."""


@dataclass(frozen=True)
class IssuedKey:
    """The one and only time the plaintext exists outside the caller."""

    plaintext: str
    key_id: str
    tenant_id: str
    prefix: str


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def issue_api_key(tenant_id: str, name: str, *, plaintext: str | None = None) -> IssuedKey:
    """Mint a key for a tenant. `plaintext` is for seeding/tests only."""
    plaintext = plaintext or f"{_KEY_PREFIX}{secrets.token_urlsafe(_KEY_BYTES)}"
    with session_scope(require_tenant=False) as session:
        if session.get(Tenant, tenant_id) is None:
            raise AuthError(f"unknown tenant {tenant_id}")
        key = ApiKey(
            tenant_id=tenant_id,
            name=name,
            key_hash=hash_key(plaintext),
            key_prefix=plaintext[:12],
        )
        session.add(key)
        session.flush()
        issued = IssuedKey(
            plaintext=plaintext, key_id=str(key.id), tenant_id=tenant_id, prefix=key.key_prefix
        )
    log.info("api key issued", extra={"tenant_id": tenant_id, "key_prefix": issued.prefix})
    return issued


def revoke_api_key(key_id: str) -> None:
    with session_scope(require_tenant=False) as session:
        key = session.get(ApiKey, key_id)
        if key is None:
            raise AuthError("unknown key")
        key.revoked_at = datetime.now(UTC)
    log.info("api key revoked", extra={"key_id": str(key_id)})


def resolve_tenant(presented: str | None) -> str:
    """Map a presented API key to its tenant, or raise AuthError.

    A revoked key and an unknown key fail identically: the caller learns only
    that the credential is not valid, never whether it once existed.
    """
    if not presented:
        raise AuthError("missing API key")
    with session_scope(require_tenant=False) as session:
        key = session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(presented)))
        if key is None or key.revoked_at is not None:
            raise AuthError("invalid API key")
        tenant = session.get(Tenant, key.tenant_id)
        if tenant is None:
            raise AuthError("invalid API key")
        return tenant.id


def tenant_status(tenant_id: str) -> str:
    with session_scope(require_tenant=False) as session:
        tenant = session.get(Tenant, tenant_id)
        return tenant.status if tenant else TenantStatus.PAUSED
