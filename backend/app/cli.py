"""Command line interface.

    uv run python -m app.cli seed --demo        load the Bengaluru fixtures
    uv run python -m app.cli seed --demo --reset   wipe first, then load
    uv run python -m app.cli check              report what is in the database

Timezone handling is the part worth reading. Fixtures express times as hours
relative to "now" in Asia/Kolkata local time, because that is how a donor
thinks ("pickup in two hours"). Those are converted to timezone-aware
timestamps here and stored as UTC by the timestamptz columns. Nothing naive
ever reaches the database.

Every row written here is tagged data_source='SIMULATED'. The column is
NOT NULL with no server default, so forgetting to tag a row is a constraint
violation rather than a silent lie in the impact dashboard.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import dispose_engine, get_sessionmaker
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.core.settings import get_settings
from app.models import (
    DataSource,
    Donation,
    DonationStatus,
    FoodCategory,
    Organization,
    OrgKind,
    QuantityUnit,
    SafetyStatus,
    User,
    UserRole,
)
from app.seed_data import (
    DEMO_PASSWORD,
    DONATIONS,
    DONOR_ORGS,
    RECIPIENT_ORGS,
    VOLUNTEERS,
)

log = get_logger("app.cli")

# Reverse dependency order, so TRUNCATE never trips a foreign key.
TABLES_IN_DELETE_ORDER = (
    "scheduled_tasks",
    "notifications",
    "workflow_events",
    "workflow_runs",
    "handover_events",
    "pickups",
    "reservations",
    "matches",
    "safety_checks",
    "donations",
    "safety_rule_versions",
    "users",
    "organizations",
)


def _point(lat: float, lon: float) -> str:
    """PostGIS WKT. Longitude comes FIRST, which is the opposite of how people
    say coordinates — getting it backwards puts Bengaluru in the sea."""
    return f"SRID=4326;POINT({lon} {lat})"


def _slug(name: str) -> str:
    """Organization name -> email local part."""
    return "".join(c.lower() if c.isalnum() else "." for c in name).strip(".")


async def _reset(session: AsyncSession) -> None:
    await session.execute(text(f"TRUNCATE {', '.join(TABLES_IN_DELETE_ORDER)} CASCADE"))
    await session.commit()
    log.info("database truncated")


async def _already_seeded(session: AsyncSession) -> bool:
    result = await session.execute(select(func.count()).select_from(Organization))
    return result.scalar_one() > 0


async def seed(*, reset: bool) -> int:
    """Load the demo fixtures. Returns a process exit code."""
    settings = get_settings()
    tz = ZoneInfo(settings.default_timezone)
    # One reference instant for the whole seed, so relative offsets stay
    # consistent even if the insert takes a few seconds.
    now_local = datetime.now(tz)

    maker = get_sessionmaker()
    async with maker() as session:
        if reset:
            await _reset(session)
        elif await _already_seeded(session):
            log.error(
                "database already contains organizations; "
                "pass --reset to wipe and reseed"
            )
            return 1

        password_hash = hash_password(DEMO_PASSWORD)

        # ---------- organizations ----------
        orgs: dict[str, Organization] = {}
        for fixture in (*DONOR_ORGS, *RECIPIENT_ORGS):
            org = Organization(
                name=fixture["name"],
                kind=OrgKind(fixture["kind"]),
                contact_email=f"contact@{_slug(fixture['name'])}.example",
                contact_phone="+91-80-0000-0000",
                address_text=fixture["address"],
                location=_point(fixture["lat"], fixture["lon"]),
                accepted_categories=(
                    [FoodCategory(c) for c in fixture["accepted_categories"]]
                    if fixture["accepted_categories"]
                    else None
                ),
                has_refrigeration=fixture["has_refrigeration"],
                daily_capacity=fixture["daily_capacity"],
                opens_at=fixture["opens_at"],
                closes_at=fixture["closes_at"],
                offers_received=fixture["offers_received"],
                offers_accepted=fixture["offers_accepted"],
                pickups_completed=fixture["pickups_completed"],
                # Only set for organizations with real history, so the
                # equity/rotation signal has something to work with.
                last_received_at=(
                    now_local - timedelta(days=3)
                    if fixture["pickups_completed"] > 0
                    else None
                ),
                is_verified=fixture["is_verified"],
                is_active=True,
                data_source=DataSource.SIMULATED,
            )
            session.add(org)
            orgs[fixture["name"]] = org

        await session.flush()  # assign org ids

        # ---------- users ----------
        # One staff account per organization, plus the volunteers and an admin.
        users_created = 0
        for fixture in DONOR_ORGS:
            session.add(
                User(
                    email=f"staff@{_slug(fixture['name'])}.example",
                    password_hash=password_hash,
                    full_name=f"{fixture['name']} Manager",
                    phone="+91-90000-00001",
                    role=UserRole.DONOR,
                    org_id=orgs[fixture["name"]].id,
                    is_available=True,
                    is_active=True,
                )
            )
            users_created += 1

        for fixture in RECIPIENT_ORGS:
            session.add(
                User(
                    email=f"staff@{_slug(fixture['name'])}.example",
                    password_hash=password_hash,
                    full_name=f"{fixture['name']} Coordinator",
                    phone="+91-90000-00002",
                    role=UserRole.RECIPIENT,
                    org_id=orgs[fixture["name"]].id,
                    is_available=True,
                    is_active=True,
                )
            )
            users_created += 1

        for volunteer in VOLUNTEERS:
            session.add(
                User(
                    email=volunteer["email"],
                    password_hash=password_hash,
                    full_name=volunteer["full_name"],
                    phone="+91-90000-00003",
                    role=UserRole.VOLUNTEER,
                    org_id=None,
                    vehicle_capacity=volunteer["vehicle_capacity"],
                    vehicle_description=volunteer["vehicle_description"],
                    is_available=True,
                    home_location=_point(volunteer["lat"], volunteer["lon"]),
                    is_active=True,
                )
            )
            users_created += 1

        session.add(
            User(
                email="admin@foodloop.example",
                password_hash=password_hash,
                full_name="FoodLoop Administrator",
                role=UserRole.ADMIN,
                org_id=None,
                is_available=True,
                is_active=True,
            )
        )
        users_created += 1

        await session.flush()

        # ---------- donations ----------
        # Map each donor org to its staff user, for created_by_user_id.
        staff_by_org: dict[str, User] = {}
        for fixture in DONOR_ORGS:
            result = await session.execute(
                select(User).where(User.org_id == orgs[fixture["name"]].id)
            )
            staff_by_org[fixture["name"]] = result.scalars().first()  # type: ignore[assignment]

        donations_created = 0
        for fixture in DONATIONS:
            donor = orgs[fixture["donor"]]
            staff = staff_by_org[fixture["donor"]]

            prepared_at = (
                now_local + timedelta(hours=fixture["prepared_offset_h"])
                if fixture["prepared_offset_h"] is not None
                else None
            )
            window_start = now_local + timedelta(hours=fixture["window_start_offset_h"])
            window_end = now_local + timedelta(hours=fixture["window_end_offset_h"])

            session.add(
                Donation(
                    donor_org_id=donor.id,
                    created_by_user_id=staff.id,
                    title=fixture["title"],
                    raw_description=fixture["raw_description"],
                    food_category=FoodCategory(fixture["category"]),
                    unit=QuantityUnit(fixture["unit"]),
                    quantity_total=fixture["quantity"],
                    quantity_reserved=Decimal("0.00"),
                    quantity_collected=Decimal("0.00"),
                    quantity_delivered=Decimal("0.00"),
                    prepared_at=prepared_at,
                    storage_notes=fixture["storage_notes"],
                    kept_refrigerated=fixture["kept_refrigerated"],
                    allergen_notes=fixture["allergen_notes"],
                    pickup_window_start=window_start,
                    pickup_window_end=window_end,
                    pickup_address_text=donor.address_text,
                    pickup_location=_point(
                        next(
                            f["lat"] for f in DONOR_ORGS if f["name"] == fixture["donor"]
                        ),
                        next(
                            f["lon"] for f in DONOR_ORGS if f["name"] == fixture["donor"]
                        ),
                    ),
                    # Deliberately NOT pre-set to the expected outcome. The
                    # safety engine must compute it, and the seed test asserts
                    # that what it computes matches the fixture's expectation.
                    status=DonationStatus.SAFETY_REVIEW,
                    safety_status=SafetyStatus.PENDING,
                    data_source=DataSource.SIMULATED,
                )
            )
            donations_created += 1

        await session.commit()

        log.info(
            "seed complete",
            extra={
                "context": {
                    "organizations": len(orgs),
                    "users": users_created,
                    "donations": donations_created,
                    "timezone": settings.default_timezone,
                }
            },
        )

    print(
        f"\nSeeded {len(orgs)} organizations, {users_created} users, "
        f"{donations_created} donations."
    )
    print(f"All rows tagged data_source=SIMULATED. Times relative to {now_local:%Y-%m-%d %H:%M %Z}.")
    print(f"\nEvery account's password: {DEMO_PASSWORD}")
    print("  donor      staff@nandini.veg.restaurant.example")
    print("  recipient  staff@annapurna.community.kitchen.example")
    print("  volunteer  ravi.volunteer@foodloop.example")
    print("  admin      admin@foodloop.example")
    print(
        "\nEvery donation is PENDING safety evaluation. Run the safety engine "
        "to resolve them;\nthe fixtures' expected outcomes are in "
        "app/seed_data.py (3 REJECTED/NEEDS_REVIEW by design).\n"
    )
    return 0


async def evaluate_all() -> int:
    """Run the safety engine over every PENDING donation.

    The same code path the API and the workflow use — this just saves logging
    in as five different donors to press the same button. Compares each
    outcome against the fixture's `expected_safety`, so it doubles as a smoke
    test of the rules engine against real seeded data.
    """
    from app.services.donation import evaluate_safety, get_donation
    from app.models.enums import SafetyStatus

    expected_by_title = {d["title"]: d["expected_safety"] for d in DONATIONS}

    maker = get_sessionmaker()
    async with maker() as session:
        result = await session.execute(
            select(Donation.id, Donation.title).where(
                Donation.safety_status == SafetyStatus.PENDING
            )
        )
        pending = result.all()

    if not pending:
        print("\nNo donations are awaiting evaluation.\n")
        return 0

    print()
    agreed = disagreed = 0
    for donation_id, title in pending:
        async with maker() as session:
            decision, _check = await evaluate_safety(session, donation_id=donation_id)

        expected = expected_by_title.get(title)
        actual = decision.outcome.value
        if expected is None:
            mark = " "
        elif expected == actual:
            mark, agreed = "+", agreed + 1
        else:
            mark, disagreed = "!", disagreed + 1

        print(f"  {mark} {actual:<13} {title[:46]:<46} {decision.summary()[:60]}")

    print()
    if disagreed:
        print(f"  {disagreed} donation(s) did NOT match the expected outcome in")
        print("  app/seed_data.py — marked with ! above. That is a rules engine bug.")
        print(f"  {agreed} matched.\n")
        return 1

    print(f"  All {agreed} evaluated donations matched their expected outcome.\n")
    return 0


async def check() -> int:
    """Report row counts and anything that looks wrong."""
    maker = get_sessionmaker()
    async with maker() as session:
        print()
        for table in reversed(TABLES_IN_DELETE_ORDER):
            result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
            print(f"  {table:24s} {result.scalar_one():>5}")

        # The invariant a sum-based check would catch if the denormalized
        # counter ever drifted from the reservation rows.
        drift = await session.execute(
            text(
                """
                SELECT d.id, d.quantity_reserved,
                       COALESCE(SUM(r.quantity) FILTER (
                           WHERE r.status IN ('HELD','CONFIRMED','CONSUMED')), 0) AS from_rows
                  FROM donations d
                  LEFT JOIN reservations r ON r.donation_id = d.id
                 GROUP BY d.id, d.quantity_reserved
                HAVING d.quantity_reserved <> COALESCE(SUM(r.quantity) FILTER (
                           WHERE r.status IN ('HELD','CONFIRMED','CONSUMED')), 0)
                """
            )
        )
        rows = drift.all()
        print()
        if rows:
            print(f"  WARNING: {len(rows)} donation(s) where quantity_reserved")
            print("  disagrees with the sum of their reservation rows:")
            for row in rows:
                print(f"    {row[0]}  counter={row[1]}  rows={row[2]}")
            return 1
        print("  quantity_reserved agrees with reservation rows on every donation.")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()

    parser = argparse.ArgumentParser(prog="app.cli", description="FoodLoop CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    seed_parser = sub.add_parser("seed", help="load demo fixtures")
    seed_parser.add_argument(
        "--demo",
        action="store_true",
        required=True,
        help="required acknowledgement that this inserts SIMULATED data",
    )
    seed_parser.add_argument(
        "--reset",
        action="store_true",
        help="truncate every table before seeding",
    )

    sub.add_parser("check", help="report row counts and check invariants")
    sub.add_parser(
        "evaluate-all",
        help="run the safety engine over every PENDING donation",
    )

    args = parser.parse_args(argv)

    async def run() -> int:
        try:
            if args.command == "seed":
                return await seed(reset=args.reset)
            if args.command == "evaluate-all":
                return await evaluate_all()
            return await check()
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
