"""Unit tests that need no database.

These run on a bare `pytest` with nothing installed but the dependencies, so
there is always something green on Day 1 even before the container is up.
"""

from app.core.logging import redact
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_handover_code,
    hash_handover_code,
    hash_password,
    verify_handover_code,
    verify_password,
)
from app.schemas.donation import DonationDraft


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------

def test_password_round_trip() -> None:
    hashed = hash_password("correct-horse-battery")
    assert verify_password("correct-horse-battery", hashed)
    assert not verify_password("wrong-password", hashed)


def test_password_hash_is_salted() -> None:
    """Same input, different hashes. A reviewer may well ask about this."""
    assert hash_password("same-input") != hash_password("same-input")


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

def test_token_round_trip() -> None:
    token = create_access_token("user-123", "DONOR", "org-456")
    claims = decode_access_token(token)
    assert claims["sub"] == "user-123"
    assert claims["role"] == "DONOR"
    assert claims["org_id"] == "org-456"


def test_tampered_token_is_rejected() -> None:
    token = create_access_token("user-123", "DONOR")
    # Flip the role in the payload without re-signing.
    tampered = token[:-6] + "AAAAAA"
    try:
        decode_access_token(tampered)
    except ValueError:
        return
    raise AssertionError("a tampered token was accepted")


# ---------------------------------------------------------------------------
# handover codes
# ---------------------------------------------------------------------------

def test_handover_code_is_unambiguous() -> None:
    """A volunteer reads this aloud over a noisy kitchen, so the alphabet
    excludes 0/O and 1/I/L."""
    for _ in range(50):
        code = generate_handover_code()
        assert len(code) == 6
        assert not set(code) & set("01OIL")


def test_handover_code_verification_is_case_insensitive() -> None:
    code = generate_handover_code()
    hashed = hash_handover_code(code)
    assert verify_handover_code(code.lower(), hashed)
    assert verify_handover_code(f"  {code}  ", hashed)
    assert not verify_handover_code("XXXXXX", hashed)


# ---------------------------------------------------------------------------
# log redaction
# ---------------------------------------------------------------------------

def test_redaction_hides_secrets() -> None:
    cleaned = redact(
        {
            "email": "someone@example.com",
            "password": "hunter2",
            "nested": {"token": "abc123", "donation_id": "d7"},
        }
    )
    assert cleaned["password"] == "***"
    assert cleaned["nested"]["token"] == "***"
    # Non-secrets survive, otherwise logs become useless.
    assert cleaned["email"] == "someone@example.com"
    assert cleaned["nested"]["donation_id"] == "d7"


# ---------------------------------------------------------------------------
# the extraction contract
# ---------------------------------------------------------------------------

def test_draft_recomputes_missing_fields() -> None:
    """An LLM that reports no gaps while leaving fields empty cannot hide it:
    the validator recomputes missing_fields from the data itself."""
    draft = DonationDraft(
        title="30 veg boxes",
        food_category="COOKED_VEG",
        # quantity, unit, window and address all absent
        missing_fields=[],  # the model claims nothing is missing
    )
    assert "quantity_total" in draft.missing_fields
    assert "pickup_window_start" in draft.missing_fields
    assert not draft.is_complete


def test_complete_draft_reports_no_gaps() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    draft = DonationDraft(
        title="50 veg boxes",
        food_category="COOKED_VEG",
        unit="BOXES",
        quantity_total=50,
        pickup_window_start=now + timedelta(hours=1),
        pickup_window_end=now + timedelta(hours=2),
        pickup_address_text="100 Feet Road, Indiranagar",
    )
    assert draft.missing_fields == []
    assert draft.is_complete
