"""HTTP 接口（FastAPI）。

端点
----
* ``POST /batches``：提交一个批次（一个或多个 unified diff 文本）。
  成功返回 200；补丁被拒绝返回 422（结构化拒绝原因）；
  版本冲突返回 409；超期返回 408。
* ``GET /files/{path}``：读取工作副本文件内容与当前版本。
* ``GET /versions``：所有文件的当前版本号。
* ``POST /recover``：显式触发崩溃恢复对账。
* ``GET /healthz``：存活探针。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .model import PatchRejected
from .service import PatchService
from .store import VersionConflict


class SubmitRequest(BaseModel):
    patches: list[str] = Field(min_length=1)
    expected_versions: dict[str, str | None] | None = None
    fuzz_radius: int | None = Field(default=None, ge=0)
    fuzz_level: int | None = Field(default=None, ge=0, le=3)
    deadline_seconds: float | None = Field(default=None, gt=0)
    batch_id: str | None = None


def create_app(workspace_dir: str | Path = "workspace") -> FastAPI:
    service = PatchService(Path(workspace_dir))
    service.recover()

    app = FastAPI(title="patch-engine", version="0.1.0")

    @app.post("/batches")
    def submit_batch(req: SubmitRequest) -> dict[str, Any]:
        try:
            result = service.submit(
                req.patches,
                expected_versions=req.expected_versions,
                fuzz_radius=req.fuzz_radius,
                fuzz_level=req.fuzz_level,
                deadline_seconds=req.deadline_seconds,
                batch_id=req.batch_id,
            )
        except VersionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "version_conflict",
                    "path": exc.path,
                    "expected": exc.expected,
                    "actual": exc.actual,
                },
            ) from exc
        except PatchRejected as exc:
            detail = exc.detail
            if detail.reason is not None and detail.reason.value == "deadline_exceeded":
                status = 408
            else:
                # 所有补丁内容层面的拒绝（含 target_missing/exists、
                # 上下文不匹配、越界、重叠、畸形补丁）统一为 422；
                # 并发版本冲突由上方 VersionConflict 单独返回 409。
                status = 422
            raise HTTPException(
                status_code=status,
                detail={
                    "error": "patch_rejected",
                    "reason": detail.reason.value if detail.reason else None,
                    "path": detail.path,
                    "message": detail.message,
                    "hunks": [
                        {
                            "hunk_index": h.hunk_index,
                            "declared_old_start": h.declared_old_start,
                            "reason": h.reason.value,
                            "message": h.message,
                        }
                        for h in detail.hunks
                    ],
                },
            ) from exc

        return {
            "batch_id": result.batch_id,
            "no_change": result.no_change,
            "versions": result.versions,
            "stats": {
                path: {
                    "elapsed_seconds": round(st.elapsed_seconds, 6),
                    "lines_scanned": st.lines_scanned,
                    "fuzzed_hunks": st.fuzzed_hunks,
                    "deadline_exceeded": st.deadline_exceeded,
                }
                for path, st in result.stats.items()
            },
        }

    @app.get("/versions")
    def versions() -> dict[str, str]:
        return service.workspace.current_versions()

    @app.get("/files/{path:path}", response_class=PlainTextResponse)
    def read_file(path: str) -> str:
        content = service.workspace.read_content(path)
        if content is None:
            raise HTTPException(status_code=404, detail="file not found")
        text = "".join(line + "\n" for line in content.lines)
        if not content.trailing_newline and content.lines:
            text = text[:-1]
        return text

    @app.post("/recover")
    def recover() -> dict[str, dict[str, str]]:
        return {"batches": service.recover()}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
