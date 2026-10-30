"""FastAPI dependencies: authentication and role-based authorization.

The rule this file enforces: **the frontend hiding a button is not security.**
Every protected endpoint carries a dependency from here, and the check happens
on the server for every request regardless of what the client sent.

`get_current_user` builds the identity from the JWT without a database read,
so the common path costs no query. `require_active_user` adds the read when an
endpoint genuinely needs to know the account is still enabled.
"""

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import Forbidden, Unauthenticated
from app.core.security import decode_access_token
from app.models import User
from app.models.enums import UserRole
from app.schemas.auth import CurrentUser

# auto_error=False so a missing header raises OUR 401 shape rather than
# FastAPI's, keeping every error response consistent for the frontend.
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    """Identity from the JWT. No database read on the common path."""
    if credentials is None or not credentials.credentials:
        raise Unauthenticated("an Authorization: Bearer <token> header is required")

    try:
        claims = decode_access_token(credentials.credentials)
    except ValueError as exc:
        raise Unauthenticated(str(exc)) from exc

    subject = claims.get("sub")
    role = claims.get("role")
    if not subject or not role:
        raise Unauthenticated("token is missing required claims")

    try:
        user_id = uuid.UUID(subject)
    except ValueError as exc:
        raise Unauthenticated("token subject is not a valid id") from exc

    org_id_raw = claims.get("org_id")
    return CurrentUser(
        user_id=user_id,
        role=UserRole(role),
        org_id=uuid.UUID(org_id_raw) if org_id_raw else None,
    )


async def require_active_user(
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Load the user row and confirm the account is still active.

    Use this where a disabled account must be locked out immediately rather
    than when their token expires — anything that writes, essentially.
    """
    result = await session.execute(select(User).where(User.id == current.user_id))
    user = result.scalars().first()
    if user is None or not user.is_active:
        raise Unauthenticated("account no longer active")
    return user


def require_roles(
    *allowed: UserRole,
) -> Callable[[CurrentUser], Coroutine[Any, Any, CurrentUser]]:
    """Dependency factory restricting an endpoint to specific roles.

        @router.post("/donations", dependencies=[Depends(require_roles(UserRole.DONOR))])

    Admins are NOT implicitly allowed everywhere — that is a deliberate
    choice. An admin wanting donor powers has to be given them explicitly, so
    nobody can reason "well, admin can do anything" about a destructive route.
    """
    allowed_set = frozenset(allowed)

    async def dependency(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current.role not in allowed_set:
            raise Forbidden(
                f"this endpoint requires one of: {', '.join(sorted(r.value for r in allowed_set))}",
                details={"your_role": current.role.value},
            )
        return current

    return dependency


async def require_admin(
    current: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if current.role != UserRole.ADMIN:
        raise Forbidden("administrator access required")
    return current


def require_org_membership(org_id: uuid.UUID, current: CurrentUser) -> None:
    """Assert the caller belongs to `org_id`, or is an admin.

    Called inside handlers rather than as a dependency because the
    organization id usually comes from the resource being fetched, which the
    dependency layer has not seen yet. Every handler that returns
    organization-scoped data must call this — it is what stops one NGO reading
    another's donations by guessing a UUID.
    """
    if current.role == UserRole.ADMIN:
        return
    if current.org_id is None or current.org_id != org_id:
        raise Forbidden(
            "you do not have access to this organization's data",
            details={"organization_id": str(org_id)},
        )


def client_ip(request: Request) -> str:
    """Best-effort client address for audit logging. Never trusted for auth."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
