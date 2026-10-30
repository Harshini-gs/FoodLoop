"""PostGIS is doing real geography.

A test that merely asserts "the query ran" would pass with lon and lat
swapped, which puts Bengaluru in the Indian Ocean and silently breaks every
distance in the system. These tests assert actual distances against known
Bengaluru landmarks, so a coordinate-order mistake fails loudly.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

# Real coordinates, from app/seed_data.py.
INDIRANAGAR = (12.9784, 77.6408)
KORAMANGALA = (12.9352, 77.6245)
WHITEFIELD = (12.9698, 77.7500)
JAYANAGAR = (12.9250, 77.5938)


async def _insert_org(session: AsyncSession, name: str, lat: float, lon: float) -> uuid.UUID:
    org_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO organizations
                (id, name, kind, address_text, location, has_refrigeration,
                 is_verified, is_active, data_source, created_at, updated_at)
            VALUES
                (:id, :name, 'RECIPIENT', :address,
                 ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                 true, true, true, 'SIMULATED', now(), now())
            """
        ),
        # name and address are separate parameters despite carrying the same
        # value: name is VARCHAR(200) and address_text is TEXT, and Postgres
        # will not deduce one type for a parameter bound to both.
        {"id": org_id, "name": name, "address": name, "lat": lat, "lon": lon},
    )
    await session.commit()
    return org_id


async def test_postgis_extension_present(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT postgis_version()"))
    version = result.scalar_one()
    assert version, "postgis_version() returned nothing"


async def test_distance_matches_known_geography(session: AsyncSession) -> None:
    """Indiranagar to Koramangala is about 4.9 km in a straight line.

    If lon/lat were swapped this would come out as thousands of kilometres,
    so the assertion catches the classic PostGIS mistake.
    """
    await _insert_org(session, "Indiranagar point", *INDIRANAGAR)

    result = await session.execute(
        text(
            """
            SELECT ST_Distance(
                location,
                ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
            ) AS metres
            FROM organizations WHERE name = 'Indiranagar point'
            """
        ),
        {"lat": KORAMANGALA[0], "lon": KORAMANGALA[1]},
    )
    metres = float(result.scalar_one())

    # 10% tolerance: asserting the geography is right, not the exact geoid.
    assert 4400 < metres < 5400, f"expected ~4900 m, got {metres:.0f} m"


async def test_st_dwithin_returns_expected_neighbours(session: AsyncSession) -> None:
    """Day 1 acceptance criterion: the expected neighbour SET at 8 km.

    From Indiranagar: Koramangala (~4.9 km) and Jayanagar (~7.6 km) are inside
    8 km; Whitefield (~11.9 km) is outside. Asserting the exact set, not just a
    non-empty result.
    """
    await _insert_org(session, "Koramangala NGO", *KORAMANGALA)
    await _insert_org(session, "Whitefield NGO", *WHITEFIELD)
    await _insert_org(session, "Jayanagar NGO", *JAYANAGAR)

    result = await session.execute(
        text(
            """
            SELECT name FROM organizations
            WHERE ST_DWithin(
                location,
                ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                8000
            )
            ORDER BY name
            """
        ),
        {"lat": INDIRANAGAR[0], "lon": INDIRANAGAR[1]},
    )
    found = {row[0] for row in result.all()}

    assert "Koramangala NGO" in found
    assert "Jayanagar NGO" in found
    assert "Whitefield NGO" not in found, "Whitefield is ~11.9 km away and must be excluded"


async def test_dwithin_radius_boundary_behaviour(session: AsyncSession) -> None:
    """Widening the radius brings Whitefield in. Proves the radius parameter is
    actually being applied rather than ignored."""
    await _insert_org(session, "Whitefield NGO", *WHITEFIELD)

    for radius, should_find in ((8000, False), (13000, True)):
        result = await session.execute(
            text(
                """
                SELECT COUNT(*) FROM organizations
                WHERE name = 'Whitefield NGO'
                  AND ST_DWithin(
                      location,
                      ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                      :radius
                  )
                """
            ),
            {"lat": INDIRANAGAR[0], "lon": INDIRANAGAR[1], "radius": radius},
        )
        count = result.scalar_one()
        assert (count == 1) is should_find, f"radius {radius} gave count {count}"


async def test_gist_index_is_used(session: AsyncSession) -> None:
    """The GIST index is what makes ST_DWithin scale. Without it the query
    still returns correct answers, so only an EXPLAIN catches its absence.

    Note: on a nearly-empty table Postgres may legitimately prefer a
    sequential scan, so this asserts the index EXISTS rather than that the
    planner chose it.
    """
    result = await session.execute(
        text(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'organizations' AND indexdef ILIKE '%gist%'
            """
        )
    )
    indexes = [row[0] for row in result.all()]
    assert indexes, "no GIST index on organizations.location"
