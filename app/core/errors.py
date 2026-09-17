"""Domain errors and their mapping to HTTP responses.

Every business failure raises one of these; a single exception handler in
``app.main`` converts them into a uniform JSON envelope:

    {"error": {"code": "INSUFFICIENT_FUNDS", "message": "..."}}
"""

from typing import Any


class AppError(Exception):
    """Base class for all expected application errors."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "Internal server error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class AuthenticationError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"
    message = "Authentication required or credentials are invalid"


class AuthorizationError(AppError):
    status_code = 403
    code = "FORBIDDEN"
    message = "You are not allowed to access this resource"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    message = "Resource not found"


class ValidationAppError(AppError):
    status_code = 422
    code = "VALIDATION_ERROR"
    message = "Request validation failed"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"
    message = "Resource state conflicts with the request"


class InsufficientFundsError(AppError):
    status_code = 402
    code = "INSUFFICIENT_FUNDS"
    message = "Agent balance is insufficient to issue this order"


class OfferNotAvailableError(AppError):
    status_code = 410
    code = "OFFER_NOT_AVAILABLE"
    message = "Offer is expired or no longer available"


class IdempotencyConflictError(AppError):
    """A concurrent request with the same Idempotency-Key is still running."""

    status_code = 409
    code = "IDEMPOTENCY_IN_PROGRESS"
    message = "A request with the same Idempotency-Key is already being processed"


class ProviderError(AppError):
    status_code = 502
    code = "PROVIDER_ERROR"
    message = "Upstream avia content provider failed"


class ProviderTimeoutError(ProviderError):
    status_code = 504
    code = "PROVIDER_TIMEOUT"
    message = "Upstream avia content provider timed out"
