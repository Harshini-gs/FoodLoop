"""The reservation transaction.

This is the most important file in the project, and the whole mechanism is
five statements inside one transaction:

    1. SELECT ... FOR UPDATE on the donation row    <- contenders queue here
    2. re-read remaining quantity under that lock   <- stale reads are the bug
    3. INSERT the reservation                       <- trigger re-checks safety
    4. UPDATE quantity_reserved                     <- CHECK constraint backstop
    5. COMMIT                                       <- lock released

Why FOR UPDATE and not SERIALIZABLE
-----------------------------------
Under READ COMMITTED with no lock, two concurrent requests both read
`remaining = 30`, both pass their check, and both insert: 60 meals promised
where 30 exist. The CHECK constraint would catch it, but as a 500-level
database error rather than a clean 409.

SERIALIZABLE would also be correct. It is rejected because it makes EVERY
write in the application a potential serialization failure needing a retry
loop, to solve contention on a single row. A row lock is the cheaper correct
answer, and it is local to the one place that needs it.

Defence in depth
----------------
Three independent mechanisms protect the same invariant, and each would catch
a bug in the others:
  * the row lock (here)                -> serializes contenders
  * the partial unique index           -> one live hold per recipient
  * the CHECK constraint on donations  -> arithmetic can never oversell
  * the BEFORE INSERT trigger          -> safety gate, independent of Python
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, ReservationConflict, SafetyGateViolation
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.models import Donation, Reservation
from app.models.enums import DonationStatus, ReservationStatus, SafetyStatus

log = get_logger(__name__)


async def _find_by_idempotency_key(
    session: AsyncSession, key: str
) -> Reservation | None:
    result = await session.execute(
        select(Reservation).where(Reservation.idempotency_key == key)
    )
    return result.scalars().first()


async def reserve(
    session: AsyncSession,
    *,
    donation_id: uuid.UUID,
    recipient_org_id: uuid.UUID,
    quantity: Decimal,
    match_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    hold_minutes: int | None = None,
) -> Reservation:
    """Reserve `quantity` of a donation. Raises ReservationConflict on a loss.

    The caller owns the transaction boundary only in the sense that this
    function commits on success; on any failure it rolls back so the row lock
    is released immediately rather than held until the request ends.
    """
    settings = get_settings()
    hold_minutes = hold_minutes or settings.reservation_hold_minutes

    if quantity <= 0:
        raise ReservationConflict("quantity must be positive", details={"quantity": str(quantity)})

    # Idempotency: a retried request returns the original reservation rather
    # than creating a second hold. The unique constraint is the enforcement;
    # this lookup is the fast path that avoids an exception.
    if idempotency_key:
        existing = await _find_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            log.info(
                "reservation replayed",
                extra={"context": {"reservation_id": str(existing.id),
                                   "idempotency_key": idempotency_key}},
            )
            return existing

    try:
        # ---- 1. the row lock. Everything depends on this line. ----
        locked = await session.execute(
            select(
                Donation.id,
                Donation.quantity_total,
                Donation.quantity_reserved,
                Donation.safety_status,
                Donation.status,
            )
            .where(Donation.id == donation_id)
            .with_for_update()
        )
        row = locked.first()

        if row is None:
            raise NotFound(f"donation {donation_id} does not exist")

        _, total, reserved, safety_status, status = row

        # ---- 2. assert against values read UNDER the lock ----
        # Anything read before the lock is stale by definition.
        if safety_status != SafetyStatus.ELIGIBLE:
            raise SafetyGateViolation(
                f"donation has safety_status={safety_status.value} and cannot be reserved",
                details={"donation_id": str(donation_id), "safety_status": safety_status.value},
            )

        if status in (
            DonationStatus.CANCELLED,
            DonationStatus.EXPIRED,
            DonationStatus.REJECTED,
            DonationStatus.COMPLETED,
        ):
            raise ReservationConflict(
                f"donation is {status.value} and no longer accepting reservations",
                details={"donation_id": str(donation_id), "status": status.value},
            )

        remaining = Decimal(total) - Decimal(reserved)
        if remaining < quantity:
            raise ReservationConflict(
                f"only {remaining} remaining, {quantity} requested",
                details={
                    "donation_id": str(donation_id),
                    "remaining": str(remaining),
                    "requested": str(quantity),
                },
            )

        # ---- 3. insert. The trigger re-checks safety independently. ----
        reservation = Reservation(
            donation_id=donation_id,
            recipient_org_id=recipient_org_id,
            match_id=match_id,
            quantity=quantity,
            status=ReservationStatus.HELD,
            expires_at=datetime.now(UTC) + timedelta(minutes=hold_minutes),
            idempotency_key=idempotency_key,
        )
        session.add(reservation)
        await session.flush()

        # ---- 4. bump the counter. The CHECK constraint is the backstop. ----
        new_reserved = Decimal(reserved) + quantity
        await session.execute(
            text(
                "UPDATE donations SET quantity_reserved = :qty, status = :status "
                "WHERE id = :id"
            ),
            {
                "qty": new_reserved,
                "status": (
                    DonationStatus.FULLY_RESERVED.value
                    if new_reserved >= Decimal(total)
                    else DonationStatus.PARTIALLY_RESERVED.value
                ),
                "id": donation_id,
            },
        )

        # ---- 5. commit, releasing the lock ----
        await session.commit()

    except IntegrityError as exc:
        await session.rollback()
        message = str(exc.orig) if exc.orig else str(exc)

        # The partial unique index fired: this recipient already holds a live
        # reservation on this donation. A replayed event, not a real conflict.
        if "uq_reservations_one_live_per_recipient" in message:
            raise ReservationConflict(
                "this recipient already holds a live reservation on this donation",
                details={"donation_id": str(donation_id)},
            ) from exc

        # The database trigger refused. Application logic should have caught
        # this already, so reaching here means something tried to bypass the
        # service layer — worth an ERROR, not a warning.
        if "safety gate" in message.lower():
            log.error(
                "safety gate refused a reservation at the database level",
                extra={"context": {"donation_id": str(donation_id)}},
            )
            raise SafetyGateViolation(
                "the database refused this reservation: donation is not ELIGIBLE",
                details={"donation_id": str(donation_id)},
            ) from exc

        # The CHECK constraint fired: the arithmetic would have oversold.
        if "ck_donations_quantities_sane" in message:
            raise ReservationConflict(
                "reserving this quantity would exceed the amount offered",
                details={"donation_id": str(donation_id)},
            ) from exc

        raise

    except Exception:
        await session.rollback()
        raise

    log.info(
        "reservation created",
        extra={"context": {
            "reservation_id": str(reservation.id),
            "donation_id": str(donation_id),
            "quantity": str(quantity),
        }},
    )
    return reservation


async def release(
    session: AsyncSession,
    *,
    reservation_id: uuid.UUID,
    reason: str,
) -> Reservation:
    """Return held quantity to the pool.

    Takes the same row lock as `reserve`, so a release racing a reservation is
    serialized rather than interleaved.
    """
    result = await session.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalars().first()
    if reservation is None:
        raise NotFound(f"reservation {reservation_id} does not exist")

    if reservation.status in (ReservationStatus.RELEASED, ReservationStatus.CONSUMED):
        # Idempotent: releasing twice is a no-op, not an error. Timer sweeps
        # and user cancellations can race.
        return reservation

    try:
        locked = await session.execute(
            select(Donation.quantity_total, Donation.quantity_reserved)
            .where(Donation.id == reservation.donation_id)
            .with_for_update()
        )
        row = locked.first()
        if row is None:
            raise NotFound(f"donation {reservation.donation_id} does not exist")

        total, reserved = row
        new_reserved = max(Decimal("0"), Decimal(reserved) - Decimal(reservation.quantity))

        reservation.status = ReservationStatus.RELEASED
        reservation.released_at = datetime.now(UTC)
        reservation.release_reason = reason[:120]

        await session.execute(
            text(
                "UPDATE donations SET quantity_reserved = :qty, status = :status "
                "WHERE id = :id"
            ),
            {
                "qty": new_reserved,
                "status": (
                    DonationStatus.ELIGIBLE.value
                    if new_reserved == 0
                    else DonationStatus.PARTIALLY_RESERVED.value
                ),
                "id": reservation.donation_id,
            },
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    log.info(
        "reservation released",
        extra={"context": {"reservation_id": str(reservation_id), "reason": reason}},
    )
    return reservation


async def confirm(
    session: AsyncSession, *, reservation_id: uuid.UUID
) -> Reservation:
    """Recipient accepted. HELD -> CONFIRMED; the hold no longer expires."""
    result = await session.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalars().first()
    if reservation is None:
        raise NotFound(f"reservation {reservation_id} does not exist")

    if reservation.status == ReservationStatus.CONFIRMED:
        return reservation   # idempotent

    if reservation.status != ReservationStatus.HELD:
        raise ReservationConflict(
            f"reservation is {reservation.status.value} and cannot be confirmed",
            details={"reservation_id": str(reservation_id)},
        )

    reservation.status = ReservationStatus.CONFIRMED
    reservation.confirmed_at = datetime.now(UTC)
    await session.commit()
    return reservation


async def expire_due_holds(session: AsyncSession, *, limit: int = 50) -> list[uuid.UUID]:
    """Release every HELD reservation past its expiry.

    Claims rows with FOR UPDATE SKIP LOCKED so several sweeper instances can
    run concurrently without fighting over the same rows.
    """
    result = await session.execute(
        select(Reservation.id)
        .where(
            Reservation.status == ReservationStatus.HELD,
            Reservation.expires_at < datetime.now(UTC),
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    expired_ids = [row[0] for row in result.all()]

    for reservation_id in expired_ids:
        await release(session, reservation_id=reservation_id, reason="hold expired")

    if expired_ids:
        log.info("expired holds released", extra={"context": {"count": len(expired_ids)}})
    return expired_ids
