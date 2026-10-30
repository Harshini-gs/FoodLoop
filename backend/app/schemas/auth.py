"""Auth request/response models.

`UserOut` deliberately omits password_hash and includes no contact details of
other users. Section 12 of the master prompt: restricted access to contact
details.
"""

import uuid

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import UserRole
from app.schemas.common import GeoPoint, ORMModel


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    role: UserRole
    org_id: uuid.UUID | None = None

    # Volunteers only.
    vehicle_capacity: float | None = Field(default=None, gt=0)
    vehicle_description: str | None = Field(default=None, max_length=120)
    home_location: GeoPoint | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: UserRole
    user_id: uuid.UUID
    org_id: uuid.UUID | None = None


class UserOut(ORMModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: UserRole
    org_id: uuid.UUID | None
    is_active: bool


class CurrentUser(BaseModel):
    """What the auth dependency returns. Built from the JWT, not a DB read, so
    the common path costs no query."""

    user_id: uuid.UUID
    role: UserRole
    org_id: uuid.UUID | None = None

    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN
