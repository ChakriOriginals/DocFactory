"""Issue (or re-issue) the local development API key and print it.

Every route but /healthz requires a key, and the only thing that seeded one was
the test suite's conftest — so `make api` by hand gave you a server you could
not talk to, and the first thing anyone trying the project hit was a 401 with
no obvious next step.

Idempotent: the same key every time, so a terminal left open keeps working
across restarts.

LOCAL ONLY, and the fixed literal is the reason. On a deployed stack call
issue_api_key without `plaintext=` so the key is random and unguessable; this
one is in the repository.
"""

import sys

from docfactory_core.auth import AuthError, issue_api_key, resolve_tenant

DEV_KEY = "dev-local-key"
DEV_TENANT = "dev-tenant"


def main() -> int:
    try:
        resolve_tenant(DEV_KEY)
    except AuthError:
        try:
            issue_api_key(DEV_TENANT, "local development", plaintext=DEV_KEY)
        except AuthError as exc:
            # Almost always "unknown tenant": migrations have not run, so the
            # seed tenant does not exist yet.
            print(f"could not issue a key: {exc}", file=sys.stderr)
            print("run `make up && make migrate` first", file=sys.stderr)
            return 1
    print(DEV_KEY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
