"""Seed fixtures: real Bengaluru coordinates, synthetic organizations.

The coordinates are real locations so that PostGIS distance tests assert
meaningful numbers — Indiranagar to Koramangala really is about 4 km, and a
test that says so catches a lon/lat swap immediately.

The organizations and people are INVENTED. Every seeded row is tagged
data_source=SIMULATED, which is why `.data_source` has no server default:
forgetting to tag a row is a constraint violation rather than a silent lie.

Timezone note: everything here is expressed in Asia/Kolkata local time and
converted to UTC on insert. One fixture deliberately crosses local midnight, so
the timezone handling is exercised by the seed itself.
"""

from datetime import time
from decimal import Decimal
from typing import TypedDict

# Asia/Kolkata. Used by the seed CLI to build timezone-aware timestamps.
IST_OFFSET_HOURS = 5.5


class OrgSeed(TypedDict):
    name: str
    kind: str
    area: str
    lat: float
    lon: float
    address: str
    accepted_categories: list[str] | None
    has_refrigeration: bool
    daily_capacity: Decimal | None
    opens_at: time | None
    closes_at: time | None
    offers_received: int
    offers_accepted: int
    pickups_completed: int
    is_verified: bool


# --- Donor organizations (restaurants, hotels, caterers, grocers) ------------
DONOR_ORGS: list[OrgSeed] = [
    {
        "name": "Nandini Veg Restaurant",
        "kind": "DONOR",
        "area": "Indiranagar",
        "lat": 12.9784,
        "lon": 77.6408,
        "address": "100 Feet Road, Indiranagar, Bengaluru 560038",
        "accepted_categories": None,
        "has_refrigeration": True,
        "daily_capacity": None,
        "opens_at": time(7, 0),
        "closes_at": time(23, 0),
        "offers_received": 0,
        "offers_accepted": 0,
        "pickups_completed": 0,
        "is_verified": True,
    },
    {
        "name": "Sattvik Caterers",
        "kind": "DONOR",
        "area": "Koramangala",
        "lat": 12.9352,
        "lon": 77.6245,
        "address": "80 Feet Road, 4th Block, Koramangala, Bengaluru 560034",
        "accepted_categories": None,
        "has_refrigeration": True,
        "daily_capacity": None,
        "opens_at": time(6, 0),
        "closes_at": time(22, 30),
        "offers_received": 0,
        "offers_accepted": 0,
        "pickups_completed": 0,
        "is_verified": True,
    },
    {
        "name": "Hotel Mayura Residency",
        "kind": "DONOR",
        "area": "Jayanagar",
        "lat": 12.9250,
        "lon": 77.5938,
        "address": "11th Main Road, 4th Block, Jayanagar, Bengaluru 560011",
        "accepted_categories": None,
        "has_refrigeration": True,
        "daily_capacity": None,
        "opens_at": time(6, 30),
        "closes_at": time(23, 30),
        "offers_received": 0,
        "offers_accepted": 0,
        "pickups_completed": 0,
        "is_verified": True,
    },
    {
        "name": "FreshMart Whitefield",
        "kind": "DONOR",
        "area": "Whitefield",
        "lat": 12.9698,
        "lon": 77.7500,
        "address": "Varthur Main Road, Whitefield, Bengaluru 560066",
        "accepted_categories": None,
        "has_refrigeration": True,
        "daily_capacity": None,
        "opens_at": time(8, 0),
        "closes_at": time(22, 0),
        "offers_received": 0,
        "offers_accepted": 0,
        "pickups_completed": 0,
        # Deliberately UNVERIFIED, so the donor_org_verified soft rule fires
        # and one seeded donation lands in NEEDS_REVIEW.
        "is_verified": False,
    },
    {
        "name": "Udupi Grand Bakery",
        "kind": "DONOR",
        "area": "Malleshwaram",
        "lat": 13.0033,
        "lon": 77.5712,
        "address": "Sampige Road, Malleshwaram, Bengaluru 560003",
        "accepted_categories": None,
        "has_refrigeration": False,
        "daily_capacity": None,
        "opens_at": time(6, 0),
        "closes_at": time(21, 0),
        "offers_received": 0,
        "offers_accepted": 0,
        "pickups_completed": 0,
        "is_verified": True,
    },
]

# --- Recipient organizations (NGOs, food banks, community kitchens) ---------
# Reliability counts vary so the matching score's reliability term actually
# discriminates between candidates rather than being constant.
RECIPIENT_ORGS: list[OrgSeed] = [
    {
        "name": "Annapurna Community Kitchen",
        "kind": "RECIPIENT",
        "area": "Indiranagar",
        "lat": 12.9719,
        "lon": 77.6412,
        "address": "CMH Road, Indiranagar, Bengaluru 560038",
        "accepted_categories": ["COOKED_VEG", "BAKERY", "PRODUCE", "PACKAGED_DRY"],
        "has_refrigeration": True,
        "daily_capacity": Decimal("200.00"),
        "opens_at": time(7, 0),
        "closes_at": time(22, 0),
        "offers_received": 40,
        "offers_accepted": 36,
        "pickups_completed": 34,
        "is_verified": True,
    },
    {
        "name": "Akshaya Trust Shelter",
        "kind": "RECIPIENT",
        "area": "Koramangala",
        "lat": 12.9304,
        "lon": 77.6180,
        "address": "5th Block, Koramangala, Bengaluru 560095",
        "accepted_categories": ["COOKED_VEG", "COOKED_NONVEG", "DAIRY", "BAKERY"],
        "has_refrigeration": True,
        "daily_capacity": Decimal("150.00"),
        "opens_at": time(8, 0),
        "closes_at": time(21, 0),
        "offers_received": 30,
        "offers_accepted": 21,
        "pickups_completed": 18,
        "is_verified": True,
    },
    {
        "name": "Seva Bharati Food Bank",
        "kind": "RECIPIENT",
        "area": "Jayanagar",
        "lat": 12.9293,
        "lon": 77.5826,
        "address": "9th Block, Jayanagar, Bengaluru 560069",
        "accepted_categories": ["PACKAGED_DRY", "PRODUCE", "BAKERY"],
        # No refrigeration: the dairy_cold_chain recipient rule excludes this
        # org from dairy donations, which gives the exclusion list real content.
        "has_refrigeration": False,
        "daily_capacity": Decimal("300.00"),
        "opens_at": time(9, 0),
        "closes_at": time(18, 0),
        "offers_received": 25,
        "offers_accepted": 22,
        "pickups_completed": 22,
        "is_verified": True,
    },
    {
        "name": "Hope Children's Home",
        "kind": "RECIPIENT",
        "area": "HSR Layout",
        "lat": 12.9116,
        "lon": 77.6474,
        "address": "Sector 2, HSR Layout, Bengaluru 560102",
        "accepted_categories": ["COOKED_VEG", "DAIRY", "BAKERY", "PRODUCE"],
        "has_refrigeration": True,
        "daily_capacity": Decimal("80.00"),
        "opens_at": time(7, 30),
        "closes_at": time(20, 0),
        "offers_received": 12,
        "offers_accepted": 11,
        "pickups_completed": 10,
        "is_verified": True,
    },
    {
        "name": "Sahaya Night Shelter",
        "kind": "RECIPIENT",
        "area": "Shivajinagar",
        "lat": 12.9850,
        "lon": 77.6050,
        "address": "Cunningham Road, Shivajinagar, Bengaluru 560001",
        "accepted_categories": ["COOKED_VEG", "COOKED_NONVEG", "PACKAGED_DRY"],
        "has_refrigeration": False,
        "daily_capacity": Decimal("120.00"),
        # Opens late and closes after midnight — this is the fixture that makes
        # the window-overlap comparison handle a wrap past 00:00.
        "opens_at": time(18, 0),
        "closes_at": time(2, 0),
        "offers_received": 18,
        "offers_accepted": 14,
        "pickups_completed": 12,
        "is_verified": True,
    },
    {
        "name": "Vatsalya Elder Care",
        "kind": "RECIPIENT",
        "area": "Malleshwaram",
        "lat": 13.0068,
        "lon": 77.5692,
        "address": "8th Cross, Malleshwaram, Bengaluru 560003",
        "accepted_categories": ["COOKED_VEG", "DAIRY", "PRODUCE"],
        "has_refrigeration": True,
        "daily_capacity": Decimal("60.00"),
        "opens_at": time(8, 0),
        "closes_at": time(19, 0),
        # No history at all: exercises the Wilson lower bound on a zero sample,
        # which must not divide by zero.
        "offers_received": 0,
        "offers_accepted": 0,
        "pickups_completed": 0,
        "is_verified": True,
    },
    {
        "name": "Mitra Welfare Society",
        "kind": "RECIPIENT",
        "area": "Whitefield",
        "lat": 12.9784,
        "lon": 77.7280,
        "address": "ITPL Main Road, Whitefield, Bengaluru 560066",
        "accepted_categories": ["COOKED_VEG", "BAKERY", "PACKAGED_DRY"],
        "has_refrigeration": True,
        "daily_capacity": Decimal("100.00"),
        "opens_at": time(9, 0),
        "closes_at": time(20, 0),
        "offers_received": 22,
        "offers_accepted": 8,
        # Poor acceptance history, so it ranks low on reliability despite
        # sometimes being nearest. Makes the ranking visibly non-trivial.
        "pickups_completed": 6,
        "is_verified": True,
    },
    {
        "name": "Karuna Relief Centre",
        "kind": "RECIPIENT",
        "area": "Rajajinagar",
        "lat": 12.9906,
        "lon": 77.5520,
        "address": "Dr Rajkumar Road, Rajajinagar, Bengaluru 560010",
        "accepted_categories": ["COOKED_VEG", "COOKED_NONVEG", "BAKERY", "PRODUCE", "DAIRY"],
        "has_refrigeration": True,
        "daily_capacity": Decimal("250.00"),
        "opens_at": time(6, 0),
        "closes_at": time(23, 0),
        "offers_received": 50,
        "offers_accepted": 47,
        "pickups_completed": 45,
        "is_verified": True,
    },
]


class VolunteerSeed(TypedDict):
    full_name: str
    email: str
    area: str
    lat: float
    lon: float
    vehicle_capacity: Decimal
    vehicle_description: str


VOLUNTEERS: list[VolunteerSeed] = [
    {
        "full_name": "Ravi Kumar",
        "email": "ravi.volunteer@foodloop.example",
        "area": "Indiranagar",
        "lat": 12.9760,
        "lon": 77.6390,
        # Smaller than several seeded donations, so capacity actually binds and
        # the "no volunteer can carry this" path is reachable.
        "vehicle_capacity": Decimal("30.00"),
        "vehicle_description": "Two-wheeler with insulated box",
    },
    {
        "full_name": "Priya Nair",
        "email": "priya.volunteer@foodloop.example",
        "area": "Koramangala",
        "lat": 12.9330,
        "lon": 77.6220,
        "vehicle_capacity": Decimal("80.00"),
        "vehicle_description": "Hatchback",
    },
    {
        "full_name": "Imran Sheikh",
        "email": "imran.volunteer@foodloop.example",
        "area": "Jayanagar",
        "lat": 12.9270,
        "lon": 77.5890,
        "vehicle_capacity": Decimal("200.00"),
        "vehicle_description": "Tempo van, refrigerated",
    },
    {
        "full_name": "Lakshmi Rao",
        "email": "lakshmi.volunteer@foodloop.example",
        "area": "Whitefield",
        "lat": 12.9720,
        "lon": 77.7400,
        "vehicle_capacity": Decimal("50.00"),
        "vehicle_description": "Three-wheeler",
    },
    {
        "full_name": "Arjun Menon",
        "email": "arjun.volunteer@foodloop.example",
        "area": "Malleshwaram",
        "lat": 13.0010,
        "lon": 77.5700,
        "vehicle_capacity": Decimal("40.00"),
        "vehicle_description": "Two-wheeler",
    },
]


# Known distances between seeded areas, for asserting that PostGIS is doing
# real geography rather than returning plausible-looking noise. Values are
# straight-line metres, computed from the coordinates above; the test asserts
# within a tolerance rather than exactly.
EXPECTED_DISTANCES_M: dict[tuple[str, str], int] = {
    ("Indiranagar", "Koramangala"): 4900,
    ("Indiranagar", "Whitefield"): 11900,
    ("Jayanagar", "Malleshwaram"): 8900,
    ("Koramangala", "HSR Layout"): 3600,
}


class DonationSeed(TypedDict):
    donor: str
    title: str
    raw_description: str | None
    category: str
    unit: str
    quantity: Decimal
    # Hours from "now" when the seed runs, in local time. Negative = past.
    prepared_offset_h: float | None
    window_start_offset_h: float
    window_end_offset_h: float
    storage_notes: str | None
    kept_refrigerated: bool | None
    allergen_notes: str | None
    # What the safety engine SHOULD return. Asserted by the seed smoke test, so
    # a regression in the rules engine is caught by running the seed.
    expected_safety: str


DONATIONS: list[DonationSeed] = [
    # ---- should clear safety cleanly ----
    {
        "donor": "Nandini Veg Restaurant",
        "title": "50 vegetarian meal boxes",
        "raw_description": "We have 50 extra veg meal boxes, pickup between 8 and 9 PM tonight.",
        "category": "COOKED_VEG",
        "unit": "BOXES",
        "quantity": Decimal("50.00"),
        "prepared_offset_h": -1.0,
        "window_start_offset_h": 0.5,
        "window_end_offset_h": 1.5,
        "storage_notes": "Held in warming counter since preparation, covered trays.",
        "kept_refrigerated": False,
        "allergen_notes": "Contains dairy (paneer) and wheat.",
        "expected_safety": "ELIGIBLE",
    },
    {
        "donor": "Sattvik Caterers",
        "title": "120 meals from cancelled event",
        "raw_description": "Wedding cancelled. 120 veg meals ready now, need pickup in 2 hours.",
        "category": "COOKED_VEG",
        "unit": "MEALS",
        "quantity": Decimal("120.00"),
        "prepared_offset_h": -0.5,
        "window_start_offset_h": 0.25,
        "window_end_offset_h": 2.0,
        "storage_notes": "Bulk hot boxes, lids sealed, not opened since cooking.",
        "kept_refrigerated": False,
        "allergen_notes": "Contains nuts, dairy, wheat.",
        "expected_safety": "ELIGIBLE",
    },
    {
        "donor": "Udupi Grand Bakery",
        "title": "40 kg assorted bread and buns",
        "raw_description": None,
        "category": "BAKERY",
        "unit": "KG",
        "quantity": Decimal("40.00"),
        "prepared_offset_h": -6.0,
        "window_start_offset_h": 0.5,
        "window_end_offset_h": 3.0,
        "storage_notes": "Cooling racks, paper-wrapped, ambient temperature.",
        "kept_refrigerated": False,
        "allergen_notes": "Wheat, egg, sesame.",
        "expected_safety": "ELIGIBLE",
    },
    # ---- should land in NEEDS_REVIEW: preparation time unknown ----
    {
        "donor": "Nandini Veg Restaurant",
        "title": "25 meal boxes, timing unclear",
        "raw_description": "Some extra boxes from lunch service, maybe 25. Can someone collect?",
        "category": "COOKED_VEG",
        "unit": "BOXES",
        "quantity": Decimal("25.00"),
        # The whole point of this fixture: NULL prepared_at must NOT pass.
        "prepared_offset_h": None,
        "window_start_offset_h": 0.5,
        "window_end_offset_h": 2.0,
        "storage_notes": None,
        "kept_refrigerated": None,
        "allergen_notes": None,
        "expected_safety": "NEEDS_REVIEW",
    },
    # ---- should land in NEEDS_REVIEW: donor organization not verified ----
    {
        "donor": "FreshMart Whitefield",
        "title": "60 kg surplus vegetables",
        "raw_description": None,
        "category": "PRODUCE",
        "unit": "KG",
        "quantity": Decimal("60.00"),
        "prepared_offset_h": None,
        "window_start_offset_h": 1.0,
        "window_end_offset_h": 4.0,
        "storage_notes": "Crates in chilled store room since morning delivery.",
        "kept_refrigerated": True,
        "allergen_notes": None,
        "expected_safety": "NEEDS_REVIEW",
    },
    # ---- should be REJECTED: past the cooked safe window ----
    {
        "donor": "Hotel Mayura Residency",
        "title": "30 non-veg meals prepared this morning",
        "raw_description": "30 chicken biryani portions from this morning's buffet.",
        "category": "COOKED_NONVEG",
        "unit": "MEALS",
        "quantity": Decimal("30.00"),
        # Prepared 9 hours ago against a 2-hour placeholder limit.
        "prepared_offset_h": -9.0,
        "window_start_offset_h": 0.5,
        "window_end_offset_h": 1.5,
        "storage_notes": "Buffet service tray, ambient.",
        "kept_refrigerated": False,
        "allergen_notes": "Contains poultry, dairy.",
        "expected_safety": "REJECTED",
    },
    # ---- dairy, needs a refrigerated recipient: shapes the exclusion list ----
    {
        "donor": "FreshMart Whitefield",
        "title": "25 litres milk nearing date",
        "raw_description": None,
        "category": "DAIRY",
        "unit": "LITRES",
        "quantity": Decimal("25.00"),
        "prepared_offset_h": None,
        "window_start_offset_h": 1.0,
        "window_end_offset_h": 5.0,
        "storage_notes": "Cold storage at 4C, unopened cartons.",
        "kept_refrigerated": True,
        "allergen_notes": "Dairy.",
        "expected_safety": "NEEDS_REVIEW",
    },
    # ---- crosses local midnight: the timezone fixture ----
    {
        "donor": "Hotel Mayura Residency",
        "title": "45 veg meals, late-night pickup",
        "raw_description": "Banquet ended late. 45 veg meals, collection around 11:30 PM to 12:30 AM.",
        "category": "COOKED_VEG",
        "unit": "MEALS",
        "quantity": Decimal("45.00"),
        # prepared_at -> window_end must stay under the 4.0 h cooked_safe_window
        # limit for COOKED_VEG, or this fixture is rejected before it ever
        # reaches matching. 0.5 + 3.0 = 3.5 h, which leaves half an hour of
        # headroom. An earlier version used -1.5 and 3.5 (5.0 h total) and was
        # correctly rejected by the engine — caught by `cli.py evaluate-all`.
        "prepared_offset_h": -0.5,
        # Deliberately placed so the window spans 00:00 IST when seeded in the
        # evening. Sahaya Night Shelter (18:00-02:00) is the only open match.
        "window_start_offset_h": 2.0,
        "window_end_offset_h": 3.0,
        "storage_notes": "Chafing dishes, covered, moved to kitchen at close.",
        "kept_refrigerated": False,
        "allergen_notes": "Dairy, wheat.",
        "expected_safety": "ELIGIBLE",
    },
    # ---- small quantity, exercises capacity fit scoring ----
    {
        "donor": "Udupi Grand Bakery",
        "title": "8 kg pastries",
        "raw_description": None,
        "category": "BAKERY",
        "unit": "KG",
        "quantity": Decimal("8.00"),
        "prepared_offset_h": -3.0,
        "window_start_offset_h": 0.25,
        "window_end_offset_h": 1.0,
        "storage_notes": "Display case, ambient, boxed at close.",
        "kept_refrigerated": False,
        "allergen_notes": "Wheat, egg, dairy, nuts.",
        "expected_safety": "ELIGIBLE",
    },
    # ---- large quantity, exceeds every single recipient's daily capacity ----
    {
        "donor": "Sattvik Caterers",
        "title": "400 meals from conference",
        "raw_description": "Large conference ended early, 400 veg meals available.",
        "category": "COOKED_VEG",
        "unit": "MEALS",
        "quantity": Decimal("400.00"),
        "prepared_offset_h": -1.0,
        "window_start_offset_h": 0.5,
        "window_end_offset_h": 2.5,
        "storage_notes": "Sealed bulk containers, hot hold since cooking.",
        "kept_refrigerated": False,
        "allergen_notes": "Dairy, wheat, nuts.",
        # Needs splitting across several recipients, so it exercises the
        # partial-allocation path.
        "expected_safety": "ELIGIBLE",
    },
]

DEMO_PASSWORD = "foodloop-demo-2026"  # noqa: S105 — seed fixture, documented in README
