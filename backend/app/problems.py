"""Errors as RFC 9457 "problem details" (``application/problem+json``).

Status codes are deliberate and consistent across the API:

* 401  no valid token
* 403  you may see this, but not do this (e.g. delete someone else's document)
* 404  it doesn't exist *or you may not see it*: we never confirm that a hidden document exists
* 409  the current state forbids it
* 422  the input breaks a rule; ``errors[]`` says which field and why
* 429  too many requests; ``Retry-After`` says when to try again
* 503  a dependency (identity provider, LLM) is unavailable
"""

import logging
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"


@dataclass
class FieldError:
    field: str
    message: str


@dataclass
class ProblemError(Exception):
    status: int
    detail: str
    errors: list[FieldError] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)


def not_found(what: str = "Document") -> ProblemError:
    return ProblemError(status.HTTP_404_NOT_FOUND, f"{what} not found")


def forbidden(detail: str = "You don't have permission to do that") -> ProblemError:
    return ProblemError(status.HTTP_403_FORBIDDEN, detail)


def invalid(field_name: str, message: str) -> ProblemError:
    return ProblemError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "The request is invalid",
        [FieldError(field_name, message)],
    )


def too_many_requests(retry_after: int) -> ProblemError:
    return ProblemError(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Too many requests. Please wait a moment.",
        headers={"Retry-After": str(retry_after)},
    )


def problem_response(
    status_code: int,
    detail: str,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": "about:blank",
        "title": _title(status_code),
        "status": status_code,
        "detail": detail,
    }
    if errors:
        body["errors"] = [{"field": e.field, "message": e.message} for e in errors]
    return JSONResponse(body, status_code=status_code, headers=headers, media_type=PROBLEM_JSON)


def _title(status_code: int) -> str:
    try:
        from http import HTTPStatus

        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def handle_problem(_: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(exc.status, exc.detail, exc.errors, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(exc.status_code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            FieldError(
                # ("body", "question") -> "question"; ("query", "limit") -> "limit"
                ".".join(str(p) for p in err.get("loc", ())[1:]) or "request",
                err.get("msg", "Invalid value"),
            )
            for err in exc.errors()
        ]
        return problem_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The request is invalid", errors
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error", exc_info=exc)
        return problem_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Something went wrong on our side."
        )
