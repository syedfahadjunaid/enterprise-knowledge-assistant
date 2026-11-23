"""Request and response bodies (pydantic models). These define the public JSON API.

Requests use ``extra="forbid"``: an unexpected field is a 422, not silently ignored. In
particular nothing in a request can name the caller, their groups or a tenant; identity comes
only from the verified token.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    filename: str
    file_type: str
    size_bytes: int
    status: str
    error: str | None
    chunk_count: int
    owner_name: str
    is_owner: bool
    can_delete: bool
    # Principals the document is shared with, besides its owner ("everyone", "group:/hr", ...)
    shared_with: list[str]
    created_at: datetime
    updated_at: datetime


class MeOut(BaseModel):
    sub: str
    username: str
    name: str
    roles: list[str]
    groups: list[str]
    principals: list[str]
    can_upload: bool
    is_admin: bool
    share_targets: list[str]


class AuditEventOut(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    actor_name: str
    action: str
    document_ids: list[uuid.UUID]
    details: dict[str, Any]
