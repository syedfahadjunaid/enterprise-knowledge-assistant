"""HTTP routes. Deliberately thin: check the role, apply the rate limit, call a service.

Services and other dependencies live on ``app.state`` (wired in ``main.create_app``), which
is what lets tests swap in fakes.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from app import audit
from app.answers.service import AnswerService
from app.documents import DocumentService
from app.problems import invalid, too_many_requests
from app.ratelimit import RateLimiter
from app.schemas import AskRequest, AuditEventOut, DocumentOut, MeOut
from app.security import ROLE_ADMIN, User, current_user, require_role

router = APIRouter(prefix="/api")

CurrentUser = Annotated[User, Depends(current_user)]


def _documents(request: Request) -> DocumentService:
    return request.app.state.documents


def _answers(request: Request) -> AnswerService:
    return request.app.state.answers


async def _enforce_rate_limit(request: Request, user: User, action: str, limit: int) -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    result = await limiter.hit(f"{action}:{user.sub}", limit)
    if not result.allowed:
        raise too_many_requests(result.retry_after)


@router.get("/me")
async def me(user: CurrentUser) -> MeOut:
    return MeOut(
        sub=user.sub,
        username=user.username,
        name=user.name,
        roles=sorted(user.roles),
        groups=list(user.groups),
        principals=list(user.principals),
        can_upload=user.can_upload,
        is_admin=user.is_admin,
        share_targets=list(user.share_targets),
    )


@router.get("/documents")
async def list_documents(
    user: CurrentUser, service: Annotated[DocumentService, Depends(_documents)]
) -> list[DocumentOut]:
    return await service.list(user)


@router.post("/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    user: CurrentUser,
    service: Annotated[DocumentService, Depends(_documents)],
    file: Annotated[UploadFile, File(description="PDF, DOCX, Markdown, HTML or plain text")],
    title: Annotated[str | None, Form()] = None,
    share_with: Annotated[list[str] | None, Form()] = None,
) -> DocumentOut:
    """Stores the file and queues it for ingestion (202: it becomes searchable shortly)."""
    settings = request.app.state.settings
    await _enforce_rate_limit(request, user, "upload", settings.upload_rate_limit_per_minute)
    # Read at most one byte more than allowed: enough to reject an oversized file without
    # loading all of it into memory.
    data = await file.read(settings.max_upload_bytes + 1)
    if not file.filename:
        raise invalid("file", "The file has no name")
    return await service.upload(
        user, filename=file.filename, data=data, title=title, share_with=share_with or []
    )


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    user: CurrentUser,
    service: Annotated[DocumentService, Depends(_documents)],
) -> DocumentOut:
    return await service.get(user, document_id)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    user: CurrentUser,
    service: Annotated[DocumentService, Depends(_documents)],
) -> Response:
    await service.delete(user, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/ask",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}},
)
async def ask(
    body: AskRequest,
    request: Request,
    user: CurrentUser,
    service: Annotated[AnswerService, Depends(_answers)],
) -> StreamingResponse:
    """Streams a cited answer built only from documents the caller may read."""
    settings = request.app.state.settings
    await _enforce_rate_limit(request, user, "ask", settings.ask_rate_limit_per_minute)
    # Retrieval and the audit record happen before the response starts, so their failures are
    # ordinary HTTP errors instead of half-sent streams.
    passages = await service.retrieve(user, body.question)
    return StreamingResponse(
        service.stream(body.question, passages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tells nginx not to buffer the stream
        },
    )


@router.get("/audit")
async def audit_log(
    request: Request,
    user: Annotated[User, Depends(require_role(ROLE_ADMIN))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditEventOut]:
    async with request.app.state.db.as_user(user) as session:
        events = await audit.recent(session, limit)
    return [
        AuditEventOut(
            id=e.id,
            occurred_at=e.occurred_at,
            actor_name=e.actor_name,
            action=e.action,
            document_ids=e.document_ids,
            details=e.details,
        )
        for e in events
    ]
