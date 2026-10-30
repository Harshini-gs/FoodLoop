"""Provider interfaces and their deterministic development implementations.

Every external dependency sits behind one of these. The default configuration
uses the mock of each, so the entire workflow runs offline with no credentials
and no network — which is also why no test needs to be skipped for lack of an
API key.

The rule that keeps this honest: **anything a mock produces is tagged
SIMULATED** on the row that stores it. `data_source` is NOT NULL with no
default, so a simulated travel time cannot be stored as though it were a live
navigation result.
"""

from app.providers.llm import LLMProvider, get_llm_provider
from app.providers.notify import NotifyProvider, get_notify_provider
from app.providers.routing import RouteEstimate, RoutingProvider, get_routing_provider

__all__ = [
    "LLMProvider",
    "NotifyProvider",
    "RouteEstimate",
    "RoutingProvider",
    "get_llm_provider",
    "get_notify_provider",
    "get_routing_provider",
]
