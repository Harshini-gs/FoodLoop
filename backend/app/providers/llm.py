"""LLM provider interface, with a deterministic mock as the default.

This is the ONLY place in the backend that can talk to a language model, and
it has exactly two jobs:

  1. `extract_donation` — turn a donor's free text into structured fields
  2. `summarize_impact`  — write prose over numbers computed elsewhere

That list is the whole of the LLM's authority in this system. It cannot clear
safety, move quantity, assign a volunteer or close a donation, because no such
method exists on the interface and no agent node is granted a tool that does.

The mock is the default. It parses with regular expressions, which is
genuinely worse than a real model at understanding messy text — and that is
fine, because the point of the default is that the workflow runs offline and
deterministically, not that extraction is clever.
"""

import json
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.core.settings import get_settings
from app.models.enums import FoodCategory, QuantityUnit
from app.schemas.donation import DonationDraft

log = get_logger(__name__)


class LLMProvider(ABC):
    """The complete surface of the LLM's authority in this system."""

    name: str = "abstract"

    @abstractmethod
    async def extract_donation(self, raw_text: str, *, now: datetime | None = None) -> DonationDraft:
        """Free text -> structured draft.

        Implementations MUST leave a field as None when it is not stated.
        Inventing a value here is the single most dangerous thing this
        interface could do: a fabricated `prepared_at` would walk straight
        through the safety gate.
        """

    @abstractmethod
    async def summarize_impact(self, metrics: dict[str, Any]) -> str:
        """Readable prose over numbers that were computed deterministically.

        Implementations receive finished figures and must not recompute,
        extrapolate or editorialize beyond them.
        """


# ---------------------------------------------------------------------------
# the mock — default, offline, deterministic
# ---------------------------------------------------------------------------

_QUANTITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(meals?|boxes?|box|kgs?|kg|kilos?|litres?|liters?|l)\b",
    re.IGNORECASE,
)
_BARE_NUMBER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")
_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", re.IGNORECASE
)

_UNIT_MAP = {
    "meal": "MEALS", "meals": "MEALS",
    "box": "BOXES", "boxes": "BOXES",
    "kg": "KG", "kgs": "KG", "kilo": "KG", "kilos": "KG",
    "litre": "LITRES", "litres": "LITRES", "liter": "LITRES", "liters": "LITRES",
    "l": "LITRES",
}

_CATEGORY_HINTS = [
    (("non-veg", "nonveg", "chicken", "mutton", "fish", "egg curry", "biryani"), "COOKED_NONVEG"),
    (("veg meal", "vegetarian", "veg box", "veg ", "sambar", "rice", "dal", "curry"), "COOKED_VEG"),
    (("bread", "bun", "pastry", "cake", "bakery", "croissant"), "BAKERY"),
    (("milk", "curd", "yoghurt", "yogurt", "paneer", "dairy", "butter"), "DAIRY"),
    (("vegetable", "fruit", "produce", "tomato", "onion", "banana"), "PRODUCE"),
    (("packet", "packaged", "dry ration", "rice bag", "flour", "biscuit"), "PACKAGED_DRY"),
]


class MockLLMProvider(LLMProvider):
    """Deterministic, offline. Same input always yields the same output.

    It extracts what it can match and leaves everything else None, which is
    exactly the behaviour required of a real provider — the difference is only
    in how much it manages to match.
    """

    name = "mock"

    async def extract_donation(
        self, raw_text: str, *, now: datetime | None = None
    ) -> DonationDraft:
        settings = get_settings()
        tz = ZoneInfo(settings.default_timezone)
        now = (now or datetime.now(UTC)).astimezone(tz)
        text_lower = raw_text.lower()

        # --- quantity and unit ---
        quantity: Decimal | None = None
        unit: str | None = None
        match = _QUANTITY_RE.search(raw_text)
        if match:
            quantity = Decimal(match.group(1))
            unit = _UNIT_MAP.get(match.group(2).lower().rstrip("s"), None) or _UNIT_MAP.get(
                match.group(2).lower()
            )
        else:
            bare = _BARE_NUMBER_RE.search(raw_text)
            if bare:
                quantity = Decimal(bare.group(1))
                # Unit genuinely unknown — leave it None rather than guessing
                # MEALS. A wrong unit silently corrupts every downstream figure.

        # --- category ---
        category: str | None = None
        for needles, value in _CATEGORY_HINTS:
            if any(needle in text_lower for needle in needles):
                category = value
                break

        # --- times ---
        times = _parse_times(raw_text, now, tz)
        window_start = times[0] if times else None
        window_end = times[1] if len(times) > 1 else None

        if window_start and not window_end:
            # A single stated time means "collect around then". One hour is an
            # assumption, so it is reported in `ambiguities` rather than
            # silently applied.
            window_end = window_start + timedelta(hours=1)

        ambiguities: list[str] = []
        normalizations: list[str] = []

        if match:
            normalizations.append(f"{match.group(0)} -> {quantity} {unit or '(unit unclear)'}")
        if quantity is not None and unit is None:
            ambiguities.append(
                f"quantity {quantity} has no stated unit (meals? boxes? kg?)"
            )
        if window_start and len(times) == 1:
            ambiguities.append(
                "only one time was stated; assumed a one-hour pickup window"
            )
        if "prepared" not in text_lower and "cooked" not in text_lower:
            ambiguities.append("preparation time was not stated")

        draft = DonationDraft(
            title=_make_title(raw_text, quantity, unit),
            food_category=category,          # type: ignore[arg-type]
            unit=unit,                       # type: ignore[arg-type]
            quantity_total=quantity,
            # prepared_at is deliberately NEVER inferred. The safety gate
            # depends on it, and a guess here would defeat the entire design.
            prepared_at=None,
            pickup_window_start=window_start,
            pickup_window_end=window_end,
            pickup_address_text=None,
            ambiguities=ambiguities,
            unit_normalizations=normalizations,
        )

        if draft.missing_fields:
            draft.clarification_question = _clarification_for(draft.missing_fields)

        log.info(
            "mock extraction",
            extra={"context": {
                "missing": draft.missing_fields,
                "ambiguities": len(ambiguities),
            }},
        )
        return draft

    async def summarize_impact(self, metrics: dict[str, Any]) -> str:
        """Template, not generation. Every number comes from `metrics` and
        none is recomputed here."""
        offered = metrics.get("quantity_offered", 0)
        delivered = metrics.get("quantity_delivered", 0)
        completed = metrics.get("donations_completed", 0)
        rate = metrics.get("recovery_rate")

        rate_text = (
            f"{float(rate) * 100:.0f}% of the food offered was delivered"
            if rate is not None
            else "a recovery rate cannot be computed (nothing was offered in this period)"
        )
        return (
            f"{completed} donation(s) completed. {offered} units were offered and "
            f"{delivered} were confirmed delivered, so {rate_text}. "
            f"Delivered figures come from verified handover records; offered "
            f"figures are what donors listed. The two are reported separately "
            f"and are not interchangeable."
        )


def _make_title(raw_text: str, quantity: Decimal | None, unit: str | None) -> str | None:
    if quantity is not None and unit:
        return f"{quantity:g} {unit.lower()}"
    first_line = raw_text.strip().splitlines()[0] if raw_text.strip() else ""
    return first_line[:200] or None


def _parse_times(raw_text: str, now: datetime, tz: ZoneInfo) -> list[datetime]:
    """Find clock times and anchor them to today (or tomorrow if already past)."""
    found: list[datetime] = []
    for match in _TIME_RE.finditer(raw_text):
        if match.group(1):
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            meridiem = (match.group(3) or "").lower()
            if meridiem == "pm" and hour != 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
        else:
            hour, minute = int(match.group(4)), int(match.group(5))

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue

        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment < now - timedelta(hours=2):
            moment += timedelta(days=1)   # "8 PM" said at 11 PM means tomorrow
        found.append(moment)

    return sorted(found)[:2]


def _clarification_for(missing: list[str]) -> str:
    friendly = {
        "quantity_total": "how much food is available",
        "unit": "what unit that is in (meals, boxes, kg, litres)",
        "food_category": "what kind of food it is",
        "pickup_window_start": "when collection can start",
        "pickup_window_end": "when collection must finish by",
        "pickup_address_text": "the pickup address",
        "title": "a short description",
    }
    parts = [friendly.get(field, field) for field in missing]
    if len(parts) == 1:
        return f"Could you tell us {parts[0]}?"
    return f"Could you tell us {', '.join(parts[:-1])}, and {parts[-1]}?"


# ---------------------------------------------------------------------------
# a real provider, enabled by configuration only
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Anthropic — a real model, same contract
# ---------------------------------------------------------------------------

#: Overridable with LLM_MODEL. A small fast model is the right default here:
#: extraction is a structured-output task, not a reasoning one, and the schema
#: plus Pydantic validation do the work that a larger model would not improve.
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"

#: The tool the model must call. Forcing a tool call is what makes the output
#: structured data rather than prose that would have to be parsed back out.
#: Note every field is nullable and `quantity_total` is a STRING: a JSON float
#: would arrive as binary floating point and 45.1 would not survive the trip to
#: Decimal intact.
_DRAFT_TOOL: dict[str, Any] = {
    "name": "record_donation_draft",
    "description": (
        "Record the donation details found in the donor's message. "
        "Every field is optional. Use null for anything the donor did not "
        "state. Do not infer, estimate or complete a value from context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": ["string", "null"],
                "description": "Short label, max 200 chars, e.g. '45 meals'.",
            },
            "food_category": {
                "type": ["string", "null"],
                "enum": [*[c.value for c in FoodCategory], None],
            },
            "unit": {
                "type": ["string", "null"],
                "enum": [*[u.value for u in QuantityUnit], None],
            },
            "quantity_total": {
                "type": ["string", "null"],
                "description": "Decimal as a string, e.g. '45' or '12.5'. Must be > 0.",
            },
            "prepared_at": {
                "type": ["string", "null"],
                "description": (
                    "ISO 8601 with offset, when the food was COOKED or PREPARED. "
                    "This one decides whether the food is safe. If the donor did "
                    "not say when it was prepared, this MUST be null."
                ),
            },
            "pickup_window_start": {"type": ["string", "null"], "description": "ISO 8601 with offset."},
            "pickup_window_end": {"type": ["string", "null"], "description": "ISO 8601 with offset."},
            "pickup_address_text": {"type": ["string", "null"]},
            "storage_notes": {"type": ["string", "null"]},
            "kept_refrigerated": {
                "type": ["boolean", "null"],
                "description": "Null unless the donor actually said either way.",
            },
            "allergen_notes": {"type": ["string", "null"]},
            "ambiguities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Anything genuinely unclear, e.g. \"'a few trays' is not a count\".",
            },
            "unit_normalizations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Conversions performed, e.g. '30 boxes -> 30 BOXES'.",
            },
            "clarification_question": {
                "type": ["string", "null"],
                "description": (
                    "One question about the MOST IMPORTANT thing still missing. "
                    "Preparation time matters most, then quantity, then whether "
                    "the food is vegetarian. Ask about those before anything else."
                ),
            },
        },
        "required": ["ambiguities", "unit_normalizations"],
    },
}

_EXTRACT_SYSTEM = """\
You extract structured donation details from a food donor's message for a food
recovery service. You are one step in a pipeline; you are not deciding anything.

TWO RULES. THEY MATTER EQUALLY.

Rule 1 — extract everything the donor DID say. "40 boxes of veg biryani" states
a quantity (40), a unit (BOXES) and a category (COOKED_VEG). Reading plain
statements out of the message is your whole job. Leaving a stated fact null is
a failure.

Rule 2 — leave a genuine gap as null. If the donor did not state something, the
field is null. Do not infer, estimate, round, or fill it in from what is
typical. A guessed `prepared_at` would walk straight through a food safety
check and put someone in hospital. Put what is unclear in `ambiguities`.

Rule 1 is about what is on the page; rule 2 is about what is not. Never use
rule 2 as an excuse to skip rule 1.

Categories: COOKED_VEG is cooked vegetarian food; COOKED_NONVEG is cooked food
containing meat, fish or egg; BAKERY is bread and baked goods; PRODUCE is raw
fruit and vegetables; DAIRY is milk products; PACKAGED_DRY is sealed
shelf-stable goods. Only when the message leaves vegetarian versus
non-vegetarian genuinely unclear, use null and ask.

`title` is a short label you write yourself from the quantity and the food,
such as "40 boxes of veg biryani".

Resolve relative times ("tonight", "by 8pm", "in an hour") against the current
time given below and emit ISO 8601 with the offset. Record each conversion in
`unit_normalizations` so a human can audit it.

A CLOCK TIME IS A STATED TIME, even when hedged. "cooked around 11 this
morning" states 11:00 — "around" makes it approximate, not absent, so extract
11:00 and note the approximation in `ambiguities`. Only a period with no clock
time in it at all ("this morning", "earlier", "after lunch") is absent.

Extract BOTH ends of a pickup window when both are given. "between 6 and 8
tonight" is a start AND an end.

TWO WORKED EXAMPLES. They differ on purpose; read both before answering.

Example 1 — "40 trays of paneer curry, cooked around 11 this morning, collect
between 6 and 8 tonight"
  title               "40 trays of paneer curry"
  food_category       "COOKED_VEG"
  unit                "BOXES"
  quantity_total      "40"
  prepared_at         today at 11:00      <- a clock time was stated
  pickup_window_start today at 18:00      <- both ends given
  pickup_window_end   today at 20:00
  ambiguities         ["'around 11' is approximate"]
  unit_normalizations ["between 6 and 8 tonight -> today 18:00-20:00",
                       "trays recorded as BOXES"]

Example 2 — "about 25 kg of leftover chapatis from this morning's breakfast
service, please collect before 7pm"
  title               "25 kg of chapatis"
  food_category       "BAKERY"
  unit                "KG"
  quantity_total      "25"
  prepared_at         null   <- "this morning's breakfast" names no clock time
  pickup_window_start null   <- only an end was given
  pickup_window_end   today at 19:00
  ambiguities         ["'about 25 kg' is approximate",
                       "'this morning' gives no exact preparation time"]
  unit_normalizations ["before 7pm -> today 19:00"]

The difference between them is the whole point: example 1 has a clock time and
two window ends, so all three are filled. Example 2 has neither, so both are
null. Decide from the message in front of you, not from whichever example you
read last.

Never invent an address.
"""

_SUMMARY_SYSTEM = """\
You write one short paragraph describing food recovery figures that have
already been computed.

You must not do arithmetic. Every number in your output must appear verbatim in
the input. Do not compute totals, percentages, differences or trends, and do not
describe the figures as good, bad, encouraging or disappointing.

State plainly that delivered figures come from verified handover records while
offered figures are what donors listed, and that the two are not
interchangeable. Three sentences at most. No headings, no bullet points.
"""


class AnthropicLLMProvider(LLMProvider):
    """Claude, confined to the same two methods as the mock.

    Nothing about using a real model widens the LLM's authority. It still
    cannot clear safety, move quantity, assign a volunteer or close a donation,
    because no such method exists on this interface.

    Three properties are load-bearing:

    * **Forced tool use.** The model must call `record_donation_draft`, so the
      output is structured data rather than prose to be parsed.
    * **Pydantic validates before anything downstream sees it.** The schema is
      a request; `DonationDraft` is the enforcement. Its `_recompute_missing`
      validator recomputes `missing_fields` from the data, so a model that
      forgets to report a gap cannot hide one.
    * **Failures raise.** No silent fall back to the mock: an application that
      quietly runs regex while reporting that it used a model is lying about
      its own provenance.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None, model: str | None, timeout_s: int) -> None:
        if not api_key:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic requires LLM_API_KEY. Set it in .env, "
                "or set LLM_PROVIDER=mock to use the offline extractor."
            )
        try:
            from anthropic import AsyncAnthropic
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "the anthropic package is not installed. Run: uv add anthropic"
            ) from exc

        self.model = model or DEFAULT_ANTHROPIC_MODEL
        self._client = AsyncAnthropic(api_key=api_key, timeout=float(timeout_s))

    async def extract_donation(
        self, raw_text: str, *, now: datetime | None = None
    ) -> DonationDraft:
        settings = get_settings()
        tz = ZoneInfo(settings.default_timezone)
        local_now = (now or datetime.now(UTC)).astimezone(tz)

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=_EXTRACT_SYSTEM,
            tools=[_DRAFT_TOOL],
            # Forced, not suggested. Without this the model may reply in prose.
            tool_choice={"type": "tool", "name": _DRAFT_TOOL["name"]},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Current time: {local_now.isoformat()} "
                        f"({settings.default_timezone})\n\n"
                        f"Donor's message:\n{raw_text}"
                    ),
                }
            ],
        )

        payload: dict[str, Any] | None = next(
            (
                dict(block.input)
                for block in response.content
                if getattr(block, "type", None) == "tool_use"
            ),
            None,
        )
        if payload is None:
            raise RuntimeError(
                "the model returned no tool call despite tool_choice being forced"
            )

        log.info(
            "llm extraction",
            extra={"context": {
                "model": self.model,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }},
        )

        # The schema asked; Pydantic enforces. An invalid draft raises here
        # rather than reaching the safety engine as plausible-looking data.
        return DonationDraft.model_validate(payload)

    async def summarize_impact(self, metrics: dict[str, Any]) -> str:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=400,
            system=_SUMMARY_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Describe these already-computed figures:\n"
                        + json.dumps(metrics, indent=2, default=str)
                    ),
                }
            ],
        )
        text = "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            raise RuntimeError("the model returned an empty summary")
        return text


# ---------------------------------------------------------------------------
# Ollama — a real model, running locally
# ---------------------------------------------------------------------------

#: Overridable with LLM_MODEL. A 3B model is enough: constrained decoding does
#: the structural work, so the model only has to read, not to format.
DEFAULT_OLLAMA_MODEL = "llama3.2:3b"


class OllamaLLMProvider(LLMProvider):
    """A model running on this machine, with the same two methods as the mock.

    Why this is a reasonable choice and not a consolation prize:

    * No API key, no account, no per-call cost, and no rate limit that can
      fail halfway through a demonstration.
    * It runs with no internet, which matches the rest of the system -- the
      routing and notification providers default to offline implementations
      for the same reason.
    * `temperature=0` and a constrained schema make extraction close to
      reproducible, which a hosted model billed per token is not.

    The cost is latency. Inference on a CPU takes seconds, not milliseconds,
    so LLM_TIMEOUT_S should be raised to about 120.
    """

    name = "ollama"

    def __init__(self, base_url: str, model: str | None, timeout_s: int) -> None:
        try:
            import httpx
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "httpx is not installed. Run: uv sync --extra llm"
            ) from exc

        self.model = model or DEFAULT_OLLAMA_MODEL
        self._httpx = httpx
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout_s)

    async def _chat(
        self, system: str, user: str, schema: dict[str, Any] | None
    ) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            # Deterministic: the same donor message should extract the same way
            # twice. A safety pipeline that answers differently on a retry is
            # not one you can reason about.
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if schema is not None:
            # Ollama constrains generation to the schema rather than merely
            # asking for it, which is what makes a 3B model usable here.
            body["format"] = schema

        try:
            async with self._httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=body)
                response.raise_for_status()
        except self._httpx.ConnectError as exc:
            raise RuntimeError(
                f"cannot reach Ollama at {self._base_url}. Is it running? "
                f"Check with: ollama list"
            ) from exc
        except self._httpx.ReadTimeout as exc:
            raise RuntimeError(
                f"Ollama did not respond within {self._timeout:.0f}s. Local "
                f"inference is slow on a CPU -- raise LLM_TIMEOUT_S."
            ) from exc

        return str(response.json()["message"]["content"])

    async def extract_donation(
        self, raw_text: str, *, now: datetime | None = None
    ) -> DonationDraft:
        settings = get_settings()
        tz = ZoneInfo(settings.default_timezone)
        local_now = (now or datetime.now(UTC)).astimezone(tz)

        content = await self._chat(
            _EXTRACT_SYSTEM,
            (
                f"Current time: {local_now.isoformat()} "
                f"({settings.default_timezone})\n\n"
                f"Donor's message:\n{raw_text}"
            ),
            # One source of truth: the same schema the Anthropic tool uses.
            schema=_DRAFT_TOOL["input_schema"],
        )

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{self.model} returned text that is not JSON despite a "
                f"constrained schema"
            ) from exc

        log.info("llm extraction", extra={"context": {"model": self.model}})

        # Same enforcement as every other provider. A local model gets no more
        # trust than a hosted one.
        return DonationDraft.model_validate(payload)

    async def summarize_impact(self, metrics: dict[str, Any]) -> str:
        content = await self._chat(
            _SUMMARY_SYSTEM,
            "Describe these already-computed figures:\n"
            + json.dumps(metrics, indent=2, default=str),
            schema=None,
        )
        text = content.strip()
        if not text:
            raise RuntimeError("the model returned an empty summary")
        return text


class HostedLLMProvider(LLMProvider):
    """Placeholder for a hosted model.

    NOT IMPLEMENTED, and deliberately raising rather than silently falling
    back to the mock: a silent fallback would mean the application claims to
    be using a real model while running regex. If you implement this, the
    contract is unchanged — unstated fields come back None, and the Pydantic
    model is validated before anything downstream sees it.
    """

    name = "hosted"

    def __init__(self, provider: str, api_key: str | None, model: str | None) -> None:
        self.provider, self.api_key, self.model = provider, api_key, model

    async def extract_donation(
        self, raw_text: str, *, now: datetime | None = None
    ) -> DonationDraft:
        raise NotImplementedError(
            f"the {self.provider!r} LLM provider is not implemented. "
            f"Set LLM_PROVIDER=mock to use the offline extractor."
        )

    async def summarize_impact(self, metrics: dict[str, Any]) -> str:
        raise NotImplementedError(
            f"the {self.provider!r} LLM provider is not implemented."
        )


_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        settings = get_settings()
        if settings.llm_provider == "mock":
            _provider = MockLLMProvider()
        elif settings.llm_provider == "anthropic":
            _provider = AnthropicLLMProvider(
                settings.llm_api_key, settings.llm_model, settings.llm_timeout_s
            )
        elif settings.llm_provider == "ollama":
            _provider = OllamaLLMProvider(
                settings.llm_base_url, settings.llm_model, settings.llm_timeout_s
            )
        else:
            _provider = HostedLLMProvider(
                settings.llm_provider, settings.llm_api_key, settings.llm_model
            )
        log.info("llm provider selected", extra={"context": {"provider": _provider.name}})
    return _provider


def reset_llm_provider() -> None:
    """Test hook."""
    global _provider
    _provider = None
