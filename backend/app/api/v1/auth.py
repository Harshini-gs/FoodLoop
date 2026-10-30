"""Authentication endpoints.

Note what `login` does NOT do: it never tells the caller whether the email
exists. A wrong password and an unknown account return the same message, so
the endpoint cannot be used to enumerate who has an account.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import get_current_user, require_active_user
from app.core.errors import Unauthenticated, ValidationFailed
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.core.settings import get_settings
from app.models import Organization, User
from app.models.enums import UserRole
from app.schemas.auth import CurrentUser, LoginRequest, RegisterRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest, session: AsyncSession = Depends(get_session)
) -> User:
    existing = await session.execute(select(User).where(User.email == payload.email))
    if existing.scalars().first() is not None:
        raise ValidationFailed("an account with this email already exists")

    if payload.role in (UserRole.DONOR, UserRole.RECIPIENT):
        if payload.org_id is None:
            raise ValidationFailed(
                f"a {payload.role.value.lower()} account must belong to an organization",
                details={"field": "org_id"},
            )
        org = await session.execute(
            select(Organization).where(Organization.id == payload.org_id)
        )
        if org.scalars().first() is None:
            raise ValidationFailed("that organization does not exist")

    # Self-registering as an administrator is refused. An admin is created by
    # the seed or promoted by another admin — otherwise the role-based access
    # control is decorative.
    if payload.role == UserRole.ADMIN:
        raise ValidationFailed("administrator accounts cannot be self-registered")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        phone=payload.phone,
        role=payload.role,
        org_id=payload.org_id,
        vehicle_capacity=payload.vehicle_capacity,
        vehicle_description=payload.vehicle_description,
        home_location=(
            payload.home_location.to_wkt() if payload.home_location else None
        ),
        is_available=True,
        is_active=True,
    )
    session.add(user)
    await session.commit()

    log.info("user registered", extra={"context": {"role": payload.role.value}})
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    result = await session.execute(select(User).where(User.email == payload.email))
    user = result.scalars().first()

    # Identical message for "no such account" and "wrong password", so this
    # endpoint cannot be used to discover who is registered.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise Unauthenticated("email or password is incorrect")

    if not user.is_active:
        raise Unauthenticated("this account has been deactivated")

    settings = get_settings()
    token = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        org_id=str(user.org_id) if user.org_id else None,
    )

    return TokenResponse(
        access_token=token,
        expires_in=int(timedelta(minutes=settings.access_token_ttl_min).total_seconds()),
        role=user.role,
        user_id=user.id,
        org_id=user.org_id,
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(require_active_user)) -> User:
    return user


@router.get("/whoami", response_model=CurrentUser)
async def whoami(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Identity straight from the token, with no database read.

    Useful for showing what the frontend thinks it is, and for demonstrating
    that authorization does not depend on anything the client sends beyond
    the signed token.
    """
    return current
