"""All enumerations, in one place.

These are created as native Postgres ENUM types by the migration. Adding a
value later needs an `ALTER TYPE ... ADD VALUE` migration, which is deliberate:
states are part of the contract, not an afterthought.

DataSource is the honesty mechanism. It is NOT NULL with no server default on
every table that can carry an estimate, so a row cannot exist without declaring
whether its numbers are real.
"""

from enum import StrEnum


class UserRole(StrEnum):
    DONOR = "DONOR"
    RECIPIENT = "RECIPIENT"
    VOLUNTEER = "VOLUNTEER"
    ADMIN = "ADMIN"


class OrgKind(StrEnum):
    DONOR = "DONOR"          # restaurant, hotel, grocery, caterer
    RECIPIENT = "RECIPIENT"  # NGO, food bank, community kitchen


class FoodCategory(StrEnum):
    COOKED_VEG = "COOKED_VEG"
    COOKED_NONVEG = "COOKED_NONVEG"
    BAKERY = "BAKERY"
    PRODUCE = "PRODUCE"
    DAIRY = "DAIRY"
    PACKAGED_DRY = "PACKAGED_DRY"


class QuantityUnit(StrEnum):
    MEALS = "MEALS"
    KG = "KG"
    BOXES = "BOXES"
    LITRES = "LITRES"


class DataSource(StrEnum):
    """Where a number came from. Never defaulted — always stated explicitly."""

    REAL = "REAL"            # measured or entered by a human in the real world
    CACHED = "CACHED"        # a real value fetched earlier and stored
    SIMULATED = "SIMULATED"  # produced by a mock provider or the seed generator
    ESTIMATED = "ESTIMATED"  # computed from a documented formula over other data


class DonationStatus(StrEnum):
    """Coarse donation lifecycle. Derived from reservations, not set freely.

    See docs/SPECS.md section 4 for the legal transition table.
    """

    DRAFT = "DRAFT"
    PENDING_INFO = "PENDING_INFO"        # clarification requested from the donor
    SAFETY_REVIEW = "SAFETY_REVIEW"
    ELIGIBLE = "ELIGIBLE"                # cleared safety, nothing reserved yet
    PARTIALLY_RESERVED = "PARTIALLY_RESERVED"
    FULLY_RESERVED = "FULLY_RESERVED"
    IN_TRANSIT = "IN_TRANSIT"
    COMPLETED = "COMPLETED"
    # terminal states below — only a human leaves these
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"


class SafetyStatus(StrEnum):
    """The gate. Only ELIGIBLE permits a reservation — enforced by a trigger.

    PENDING  : not yet evaluated.
    ELIGIBLE : every hard rule passed with the evidence present.
    NEEDS_REVIEW : a rule could not be evaluated, or a soft rule failed.
                   Missing evidence lands HERE, never in ELIGIBLE.
    REJECTED : a hard rule failed. Not appealable by any agent.
    """

    PENDING = "PENDING"
    ELIGIBLE = "ELIGIBLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"


class MatchStatus(StrEnum):
    PROPOSED = "PROPOSED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ReservationStatus(StrEnum):
    """The only entity that moves quantity.

    HELD      : counted against quantity_reserved, expires at expires_at.
    CONFIRMED : recipient accepted; still counted.
    CONSUMED  : a verified collection happened.
    RELEASED  : returned to the pool (declined, timed out, or cancelled).
    """

    HELD = "HELD"
    CONFIRMED = "CONFIRMED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class PickupStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    EN_ROUTE = "EN_ROUTE"
    COLLECTED = "COLLECTED"
    DELIVERED = "DELIVERED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class HandoverKind(StrEnum):
    COLLECT = "COLLECT"  # donor -> volunteer
    DELIVER = "DELIVER"  # volunteer -> recipient


class NotificationStatus(StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"  # exceeded max attempts


class TaskKind(StrEnum):
    EXPIRE_RESERVATION = "EXPIRE_RESERVATION"
    CONFIRMATION_TIMEOUT = "CONFIRMATION_TIMEOUT"
    RETRY_NOTIFICATION = "RETRY_NOTIFICATION"
    EXPIRE_DONATION = "EXPIRE_DONATION"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class WorkflowRunStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_TIMER = "WAITING_TIMER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EventPhase(StrEnum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    ERROR = "ERROR"
    RETRY = "RETRY"
    INTERRUPT = "INTERRUPT"
