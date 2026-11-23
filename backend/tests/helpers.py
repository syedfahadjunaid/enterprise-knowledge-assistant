"""Helpers shared by the API tests."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.ingestion.worker import IngestionWorker
from tests.conftest import Persona, auth


@dataclass
class SseResult:
    events: list[tuple[str, Any]] = field(default_factory=list)

    def of(self, name: str) -> list[Any]:
        return [data for event, data in self.events if event == name]

    @property
    def sources(self) -> list[dict[str, Any]]:
        return self.of("sources")[0]

    @property
    def answer(self) -> str:
        return "".join(d["text"] for d in self.of("delta"))

    @property
    def done(self) -> dict[str, Any]:
        return self.of("done")[0]


def parse_sse(body: str) -> SseResult:
    result = SseResult()
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        result.events.append((lines["event"], json.loads(lines["data"])))
    return result


async def upload(
    api: httpx.AsyncClient,
    persona: Persona,
    filename: str,
    content: bytes | str,
    *,
    share_with: tuple[str, ...] = (),
    title: str | None = None,
) -> httpx.Response:
    data: dict[str, Any] = {"share_with": list(share_with)}
    if title is not None:
        data["title"] = title
    body = content.encode() if isinstance(content, str) else content
    return await api.post(
        "/api/documents",
        headers=auth(persona),
        files={"file": (filename, body)},
        data=data,
    )


async def upload_ready(
    api: httpx.AsyncClient,
    worker: IngestionWorker,
    persona: Persona,
    filename: str,
    content: bytes | str,
    *,
    share_with: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Uploads a document and runs the worker until it is ingested."""
    response = await upload(api, persona, filename, content, share_with=share_with)
    assert response.status_code == 202, response.text
    await worker.run_until_idle()
    document = await api.get(f"/api/documents/{response.json()['id']}", headers=auth(persona))
    assert document.json()["status"] == "READY", document.json()
    return document.json()


async def ask(api: httpx.AsyncClient, persona: Persona, question: str) -> SseResult:
    response = await api.post("/api/ask", headers=auth(persona), json={"question": question})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)
