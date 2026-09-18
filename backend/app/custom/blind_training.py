"""Blind trading training extension route adapter."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services import blind_training
from app.services.ai_provider import stream_ai_text

EXTENSION_ID = "blind.training"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
router = APIRouter(prefix="/api/blind-training", tags=["blind-training"])


class ActionRequest(BaseModel):
    side: str
    percentage: float = Field(gt=0, le=100)


class StartRequest(BaseModel):
    commission_pct: float = Field(default=blind_training.FEES_PCT, ge=0, le=0.05)
    stamp_tax_pct: float = Field(default=blind_training.STAMP_TAX_PCT, ge=0, le=0.05)
    slippage_bps: float = Field(default=blind_training.SLIPPAGE_BPS, ge=0, le=1000)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc))
    return HTTPException(400, str(exc))


@router.post("/sessions")
def create_session(request: Request, body: StartRequest | None = None) -> dict[str, Any]:
    try:
        body = body or StartRequest()
        return {
            "session": blind_training.create_session(
                request.app.state.repo,
                body.model_dump(),
            )
        }
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/sessions/{session_id}")
def session(session_id: str) -> dict[str, Any]:
    try:
        return {"session": blind_training.snapshot_session(session_id)}
    except Exception as exc:
        raise _error(exc) from exc


@router.delete("/sessions/{session_id}")
def discard_session(session_id: str) -> dict[str, Any]:
    try:
        return {"removed": blind_training.discard_session(session_id)}
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/sessions/{session_id}/next")
def next_bar(session_id: str) -> dict[str, Any]:
    try:
        return {"session": blind_training.next_bar(session_id)}
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/sessions/{session_id}/action")
def action(session_id: str, body: ActionRequest) -> dict[str, Any]:
    try:
        return blind_training.execute_action(session_id, body.side, body.percentage)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/sessions/{session_id}/finish")
def finish(session_id: str) -> dict[str, Any]:
    try:
        return blind_training.finish_session(session_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/records")
def records() -> dict[str, Any]:
    return {"records": blind_training.list_records()}


@router.get("/records/{training_id}")
def record(training_id: str, request: Request) -> dict[str, Any]:
    try:
        return {"record": blind_training.get_record(training_id, request.app.state.repo)}
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/records/{training_id}/replay")
def replay_record(training_id: str, request: Request) -> dict[str, Any]:
    try:
        return {
            "session": blind_training.replay_record(
                request.app.state.repo,
                training_id,
            )
        }
    except Exception as exc:
        raise _error(exc) from exc


@router.delete("/records/{training_id}")
def delete_record(training_id: str) -> dict[str, Any]:
    try:
        return {"removed": blind_training.delete_record(training_id)}
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/records/{training_id}/analyze")
async def analyze(training_id: str, request: Request) -> StreamingResponse:
    try:
        record = blind_training.get_record(training_id, request.app.state.repo)
    except Exception as exc:
        raise _error(exc) from exc

    async def stream() -> Any:
        content = ""
        try:
            yield json.dumps({"type": "meta", "training_id": training_id}, ensure_ascii=False) + "\n"
            async for chunk in stream_ai_text(blind_training.build_ai_messages(record), temperature=0.3, max_tokens=3000):
                content += chunk
                yield json.dumps({"type": "delta", "content": chunk}, ensure_ascii=False) + "\n"
            if not content.strip():
                raise RuntimeError("AI 未返回有效分析内容")
            report = blind_training.record_store().save_report({
                "training_id": record.get("training_id"),
                "symbol": record.get("symbol", ""),
                "name": record.get("name", ""),
                "content": content,
                "created_at": None,
            })
            yield json.dumps({"type": "done", "report": report}, ensure_ascii=False) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
