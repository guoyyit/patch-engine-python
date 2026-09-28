"""FastAPI HTTP surface.

Endpoints
---------
``GET  /health``                liveness + counters
``POST /recover``               run crash recovery
``GET  /files/{path:path}``     read a tracked file
``GET  /files/{path:path}/status``  version / blob / size
``POST /batches``               submit one atomic batch of unified diffs

Batch request body::

    {
      "diffs": ["--- ...\\n+++ ...\\n@@ ...", ...],
      "base_versions": {"path/to/file": 3},   // optional optimistic lock
      "batch_id": "client-supplied-id",       // optional, for idempotency
      "deadline_ms": 500                       // optional processing budget
    }
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .models import (
    ConflictError,
    DeadlineExceeded,
    PatchError,
    PatchParseError,
    PatchRejected,
)


class BatchRequest(BaseModel):
    diffs: list[str] = Field(min_length=1)
    base_versions: dict[str, int] | None = None
    batch_id: str | None = None
    deadline_ms: float | None = None


def _appl_to_dict(a) -> dict[str, Any]:  # noqa: ANN001
    return {
        "path": a.path,
        "old_version": a.old_version,
        "new_version": a.new_version,
        "hunks": [
            {
                "hunk_index": h.hunk_index,
                "declared_start": h.old_start_declared,
                "applied_at": h.applied_at,
                "offset": h.offset,
                "fuzz": h.fuzz,
            }
            for h in a.hunks
        ],
    }


def create_app(service=None) -> FastAPI:  # noqa: ANN001
    app = FastAPI(title="patch-engine", version="0.1.0")

    if service is None:
        root = os.environ.get("PATCH_ENGINE_ROOT", ".patch-engine")
        from .service import PatchService

        service = PatchService(root)
        service.recover()

    @app.exception_handler(PatchParseError)
    async def parse_handler(_request, exc: PatchParseError):
        return JSONResponse(
            status_code=422,
            content={
                "error": "patch_parse_error",
                "reason": exc.reason.value,
                "message": str(exc),
                "line": exc.line,
            },
        )

    @app.exception_handler(PatchRejected)
    async def reject_handler(_request, exc: PatchRejected):
        return JSONResponse(
            status_code=409,
            content={
                "error": "patch_rejected",
                "reason": exc.reason.value,
                "message": str(exc),
                "file": exc.file,
                "hunk_index": exc.hunk_index,
                "hunk_start": exc.hunk_start,
            },
        )

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request, exc: ConflictError):
        return JSONResponse(
            status_code=409,
            content={
                "error": "conflict",
                "message": str(exc),
                "file": exc.file,
                "expected": exc.expected,
                "actual": exc.actual,
                "retryable": True,
            },
        )

    @app.exception_handler(DeadlineExceeded)
    async def deadline_handler(_request, exc: DeadlineExceeded):
        return JSONResponse(
            status_code=504,
            content={"error": "deadline_exceeded", "message": str(exc)},
        )

    @app.exception_handler(PatchError)
    async def patch_error_handler(_request, exc: PatchError):
        return JSONResponse(
            status_code=400, content={"error": "patch_error", "message": str(exc)}
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "metrics": dict(service.metrics)}

    @app.post("/recover")
    def recover():
        return service.recover()

    @app.get("/files/{path:path}/status")
    def get_status(path: str):
        return service.status(path)

    @app.get("/files/{path:path}")
    def get_file(path: str):
        try:
            content = service.read_file(path)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"unknown file: {path}")
        return {"path": path, "content": content}

    @app.post("/batches")
    def submit_batch(req: BatchRequest):
        result = service.submit_batch(
            req.diffs,
            base_versions=req.base_versions,
            batch_id=req.batch_id,
            deadline_ms=req.deadline_ms,
        )
        return {
            "batch_id": result.batch_id,
            "elapsed_ms": result.elapsed_ms,
            "applied": [_appl_to_dict(a) for a in result.applied],
        }

    return app


app = create_app()
