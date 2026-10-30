"""Model package.

Importing this module registers every table on `Base.metadata`, which is what
Alembic's env.py needs for autogenerate to see the full schema. If you add a
model module, import it here or migrations will silently miss it.
"""

from app.models.base import Base
from app.models.donation import Donation, SafetyCheck, SafetyRuleVersion
from app.models.enums import (
    DataSource,
    DonationStatus,
    EventPhase,
    FoodCategory,
    HandoverKind,
    MatchStatus,
    NotificationStatus,
    OrgKind,
    PickupStatus,
    QuantityUnit,
    ReservationStatus,
    SafetyStatus,
    TaskKind,
    TaskStatus,
    UserRole,
    WorkflowRunStatus,
)
from app.models.fulfilment import HandoverEvent, Match, Pickup, Reservation
from app.models.identity import Organization, User
from app.models.operations import (
    NotificationRecord,
    ScheduledTask,
    WorkflowEvent,
    WorkflowRun,
)

__all__ = [
    "Base",
    # identity
    "Organization",
    "User",
    # donation
    "Donation",
    "SafetyCheck",
    "SafetyRuleVersion",
    # fulfilment
    "HandoverEvent",
    "Match",
    "Pickup",
    "Reservation",
    # operations
    "NotificationRecord",
    "ScheduledTask",
    "WorkflowEvent",
    "WorkflowRun",
    # enums
    "DataSource",
    "DonationStatus",
    "EventPhase",
    "FoodCategory",
    "HandoverKind",
    "MatchStatus",
    "NotificationStatus",
    "OrgKind",
    "PickupStatus",
    "QuantityUnit",
    "ReservationStatus",
    "SafetyStatus",
    "TaskKind",
    "TaskStatus",
    "UserRole",
    "WorkflowRunStatus",
]
