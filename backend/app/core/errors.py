"""Domain exceptions and their HTTP mapping.

`ReservationConflict` is the one to look at: it maps to 409 and is what the
losing side of a concurrent reservation receives. The workflow's reservation
conflict branch is driven by catching exactly this.

`SafetyGateViolation` is raised when the database trigger refuses an insert.
It maps to 409 too, but it should never be reachable through the API — if it
fires, something tried to bypass the gate and that is worth an ERROR log.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


class FoodLoopError(Exception):
    """Base class. `code` is a stable machine-readable string for the frontend."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "foodloop_error"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(FoodLoopError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ValidationFailed(FoodLoopError):
    status_code: int = 422   # literal: Starlette renamed the constant across versions
    code = "validation_failed"


class Unauthenticated(FoodLoopError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"


class Forbidden(FoodLoopError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class ReservationConflict(FoodLoopError):
    """Lost the race for quantity, or the quantity changed under us."""

    status_code = status.HTTP_409_CONFLICT
    code = "reservation_conflict"


class SafetyGateViolation(FoodLoopError):
    """An operation was attempted on a donation that has not cleared safety."""

    status_code = status.HTTP_409_CONFLICT
    code = "safety_gate_violation"


class InvalidStateTransition(FoodLoopError):
    status_code = status.HTTP_409_CONFLICT
    code = "invalid_state_transition"


class ProviderUnavailable(FoodLoopError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "provider_unavailable"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(FoodLoopError)
    async def _handle(request: Request, exc: FoodLoopError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )
