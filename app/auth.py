"""Internal login hook.

call-trace has no web session, and neither does ct-tools. The page route
depends on ``require_internal_user``. When this repository grows an internal
login, enforce it here. Do not add a new SSO in this module.
"""

from __future__ import annotations


def require_internal_user() -> None:
    """Allow the request through until an internal login is plugged in."""
    return None
