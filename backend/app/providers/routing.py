"""Routing provider: distance and travel-time estimates.

Every estimate carries a `data_source`, and the simulated provider returns
`SIMULATED`. That value is stored on the pickup row in a column that is
NOT NULL with no default, so a seeded straight-line estimate can never be
displayed as though it were live navigation.

The simulated provider is a great-circle distance multiplied by a road factor.
It is a reasonable approximation for a city and completely wrong across a
river or a railway line — which is precisely why it is labelled.
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.settings import get_settings
from app.models.enums import DataSource

log = get_logger(__name__)

EARTH_RADIUS_M = 6_371_000.0

# Bengaluru traffic, very roughly, for the simulated provider only.
ASSUMED_SPEED_KMH = 18.0


@dataclass(frozen=True)
class RouteEstimate:
    distance_km: float
    duration_min: int
    data_source: DataSource
    provider: str

    @property
    def is_live(self) -> bool:
        return self.data_source in (DataSource.REAL, DataSource.CACHED)

    def label(self) -> str:
        """What the UI shows beside the number. Never omit this."""
        return {
            DataSource.REAL: "live route",
            DataSource.CACHED: "cached route",
            DataSource.SIMULATED: "estimated, not live navigation",
            DataSource.ESTIMATED: "estimated",
        }[self.data_source]


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Straight line, ignoring roads entirely."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class RoutingProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def estimate(
        self, *, from_lat: float, from_lon: float, to_lat: float, to_lon: float
    ) -> RouteEstimate:
        ...


class SimulatedRoutingProvider(RoutingProvider):
    """Haversine x a road factor. Deterministic, offline, clearly labelled."""

    name = "simulated"

    async def estimate(
        self, *, from_lat: float, from_lon: float, to_lat: float, to_lon: float
    ) -> RouteEstimate:
        settings = get_settings()
        straight_m = haversine_meters(from_lat, from_lon, to_lat, to_lon)
        road_km = (straight_m / 1000.0) * settings.road_factor
        duration = max(1, round((road_km / ASSUMED_SPEED_KMH) * 60))

        return RouteEstimate(
            distance_km=round(road_km, 2),
            duration_min=duration,
            # The honesty mechanism, at the point the number is created.
            data_source=DataSource.SIMULATED,
            provider=self.name,
        )


class OSRMRoutingProvider(RoutingProvider):
    """A real routing engine. NOT IMPLEMENTED.

    Raises rather than falling back to simulation, so the application can
    never silently present an estimate as a live route.
    """

    name = "osrm"

    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url

    async def estimate(
        self, *, from_lat: float, from_lon: float, to_lat: float, to_lon: float
    ) -> RouteEstimate:
        raise NotImplementedError(
            "the OSRM routing provider is not implemented. "
            "Set ROUTING_PROVIDER=simulated to use offline estimates."
        )


_provider: RoutingProvider | None = None


def get_routing_provider() -> RoutingProvider:
    global _provider
    if _provider is None:
        settings = get_settings()
        if settings.routing_provider == "simulated":
            _provider = SimulatedRoutingProvider()
        else:
            _provider = OSRMRoutingProvider(settings.osrm_base_url)
        log.info("routing provider selected", extra={"context": {"provider": _provider.name}})
    return _provider


def reset_routing_provider() -> None:
    global _provider
    _provider = None
