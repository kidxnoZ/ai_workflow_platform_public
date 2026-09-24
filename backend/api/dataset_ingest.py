"""backend/api/dataset_ingest.py

HTTP + SSE endpoints for the dataset ingest Agent.
SSE endpoint streams task.logs at 200ms poll interval.
All write operations go through DatasetIngestExecutor via the Worker queue.
"""
from __future__ import annotations
import json
import os
import shutil
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.config import STORAGE_DIR
from backend.models.task import Task, File
from backend.schemas.dataset_ingest import (
    IngestRequest, IngestResponse,
    ActionRequest, ActionResponse,
    DirTreeResponse, DirTreeNode,
    PreviewResponse, PreviewSample, ParamGroupPreview,
    ReviewItemsResponse, ReviewItem,
)

router = APIRouter(prefix="/api/dataset-ingest", tags=["dataset-ingest"])


# ---------------------------------------------------------------------------
# POST /
# ---------------------------------------------------------------------------

@router.post("", response_model=IngestResponse)
async def create_ingest_task(
    req: IngestRequest,
    db: Session = Depends(get_db),
) -> IngestResponse:
    """Create a new dataset ingest task; returns task_id.
    Validates source_path exists (server_path mode) or upload_file_id (upload mode)."""
    if req.source_type == "server_path":
        if not req.source_path or not os.path.exists(req.source_path):
            raise HTTPException(status_code=400, detail="source_path does not exist")
    elif req.source_type == "upload":
        if not req.upload_file_id:
            raise HTTPException(status_code=400, detail="upload_file_id is required for upload mode")
        file_record = db.get(File, req.upload_file_id)
        if not file_record:
            raise HTTPException(status_code=400, detail=f"Upload file {req.upload_file_id} not found")

    input_data = {
        "phase": "start",
        "source_type": req.source_type,
        "source_path": req.source_path,
        "output_path": req.output_path,
        "project_code": req.project_code,
        "source_label": req.source_label,
        "copyright": req.copyright,
        "domain": req.domain,
        "language": req.language,
        "steps": req.steps,
    }

    if req.source_type == "upload":
        file_record = db.get(File, req.upload_file_id)
        input_data["upload_file_id"] = req.upload_file_id
        input_data["upload_path"] = file_record.stored_path

    task = Task(
        type="dataset_ingest",
        status="pending",
        input=json.dumps(input_data, ensure_ascii=False),
    )
    db.add(task)
    db.commit()
    return IngestResponse(task_id=task.id)


# ---------------------------------------------------------------------------
# GET /stream/{task_id}  (SSE)
# ---------------------------------------------------------------------------

@router.get("/stream/{task_id}")
async def stream_task_events(
    task_id: str,
    db: Session = Depends(get_db),
):
    """SSE endpoint. Polls task.logs every 200ms and pushes new entries as events.
    Returns EventSourceResponse (sse-starlette)."""

    async def event_generator():
        last_idx = 0
        while True:
            db.expire_all()
            task = db.get(Task, task_id)
            if task is None:
                break

            logs = json.loads(task.logs or "[]")
            for entry in logs[last_idx:]:
                msg = entry.get("msg", "")
                try:
                    parsed = json.loads(msg)
                    if "type" in parsed:
                        # Route to named channel so the frontend can use
                        # addEventListener('thinking', …) / addEventListener('message', …)
                        event_type = "thinking" if parsed.get("type") == "thinking" else "message"
                        yield {"event": event_type, "data": msg}
                    last_idx += 1
                except (json.JSONDecodeError, TypeError):
                    last_idx += 1

            if task.status in ("done", "error", "failed"):
                # Drain remaining entries then close
                break
            await asyncio.sleep(0.2)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# POST /{task_id}/action
# ---------------------------------------------------------------------------

@router.post("/{task_id}/action", response_model=ActionResponse)
async def submit_action(
    task_id: str,
    req: ActionRequest,
    db: Session = Depends(get_db),
) -> ActionResponse:
    """Handle user decisions: approve/reject a phase, submit review items,
    or trigger a manual file move. Sets task.status back to 'pending' on approve."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")

    if req.action in ("approve", "reject"):
        input_data["user_action"] = req.dict()
        task.status = "pending"
        task.updated_at = datetime.utcnow()

    elif req.action == "resubmit_script":
        # User manually edited the failed script — send back for sandbox re-check
        input_data["user_action"] = {"action": "resubmit_script", "script": req.feedback or ""}
        task.status = "pending"
        task.updated_at = datetime.utcnow()

    elif req.action == "sandbox_override":
        # Dev mode: skip static analysis layers, run directly with runtime SafeShutil only
        input_data["user_action"] = {"action": "sandbox_override"}
        task.status = "pending"
        task.updated_at = datetime.utcnow()

    elif req.action == "submit_review":
        input_data["review_items"] = req.review_items
        task.status = "pending"
        task.updated_at = datetime.utcnow()

    elif req.action == "manual_move":
        output_path = input_data.get("output_path", "")
        if not req.move_src or not req.move_dst:
            raise HTTPException(status_code=400, detail="move_src and move_dst are required for manual_move")
        src_path = Path(req.move_src)
        if not src_path.exists():
            raise HTTPException(status_code=400, detail=f"Source file not found: {req.move_src}")
        dst_dir = Path(output_path) / req.move_dst / "audio"
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_dir / src_path.name))
        task.status = "pending"
        task.updated_at = datetime.utcnow()

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

    task.input = json.dumps(input_data, ensure_ascii=False)
    db.commit()
    return ActionResponse(ok=True)


# ---------------------------------------------------------------------------
# POST /{task_id}/cancel  — terminate a running/pending/awaiting_review task
# ---------------------------------------------------------------------------

@router.post("/{task_id}/cancel", response_model=ActionResponse)
async def cancel_task(
    task_id: str,
    db: Session = Depends(get_db),
) -> ActionResponse:
    """Mark task as failed immediately. Worker will stop processing on next check."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status in ("done", "success", "failed"):
        return ActionResponse(ok=True, message="Task already finished")
    task.status = "failed"
    task.error = "用户手动终止"
    task.finished_at = datetime.utcnow()
    task.updated_at = datetime.utcnow()
    db.commit()
    # Clean up preview_cache on cancel
    import shutil as _shutil
    preview_cache = STORAGE_DIR / "ingest_sessions" / task_id / "preview_cache"
    if preview_cache.exists():
        _shutil.rmtree(str(preview_cache), ignore_errors=True)
    return ActionResponse(ok=True)


# ---------------------------------------------------------------------------
# GET /{task_id}/tree
# ---------------------------------------------------------------------------

@router.get("/{task_id}/tree", response_model=DirTreeResponse)
async def get_dir_tree(
    task_id: str,
    db: Session = Depends(get_db),
) -> DirTreeResponse:
    """Return the current directory tree from task.input['dir_tree']."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    dir_tree = input_data.get("dir_tree")

    if dir_tree is None:
        return DirTreeResponse(
            tree=DirTreeNode(name="root", type="dir", file_count=0, children=[]),
            role_count=0,
            special_count=0,
            integrity_ok=False,
            integrity_detail="",
        )

    return DirTreeResponse(**dir_tree)


# ---------------------------------------------------------------------------
# GET /{task_id}/preview
# ---------------------------------------------------------------------------

@router.get("/{task_id}/preview", response_model=PreviewResponse)
async def get_preview(
    task_id: str,
    step: str = Query(...),
    db: Session = Depends(get_db),
) -> PreviewResponse:
    """Return per-group before/after audio sample URLs for a processing step."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    preview_key = f"{step}_preview"   # executor writes as "{step_name}_preview"
    preview_data = input_data.get(preview_key)

    if preview_data is None:
        return PreviewResponse(step=step, groups=[])

    # preview_data is a list of group dicts written by _phase_audio_preprocess
    groups = []
    for g in (preview_data if isinstance(preview_data, list) else []):
        # executor stores "representative_params", schema uses "params"
        params = g.get("representative_params") or g.get("params") or {}
        samples = []
        for s in g.get("samples", []):
            if isinstance(s, dict):
                # executor now stores dicts with before_url/after_url (after preview_cache impl)
                samples.append(PreviewSample(
                    key=s.get("key", ""),
                    speaker_id=s.get("speaker_id", g.get("group_id", "")),
                    before_url=s.get("before_url", ""),
                    after_url=s.get("after_url", ""),
                    duration_sec=float(s.get("duration_sec", 0.0)),
                ))
            elif isinstance(s, str):
                # legacy: raw file paths (before preview_cache impl)
                fname = Path(s).name
                samples.append(PreviewSample(
                    key=Path(s).stem,
                    speaker_id=g.get("group_id", ""),
                    before_url=f"/api/dataset-ingest/{task_id}/audio?path={s}",
                    after_url=f"/api/dataset-ingest/{task_id}/audio?path={s}",
                    duration_sec=0.0,
                ))
        groups.append(ParamGroupPreview(
            group_id=g.get("group_id", ""),
            speaker_ids=g.get("speaker_ids", []),
            params=params,
            samples=samples,
        ))

    return PreviewResponse(step=step, groups=groups)


# ---------------------------------------------------------------------------
# GET /{task_id}/review-items
# ---------------------------------------------------------------------------

@router.get("/{task_id}/review-items", response_model=ReviewItemsResponse)
async def get_review_items(
    task_id: str,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, le=200),
    db: Session = Depends(get_db),
) -> ReviewItemsResponse:
    """Return paginated review items from task.input['review_items']."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    all_items = input_data.get("review_items") or []

    total = len(all_items)
    start = (page - 1) * size
    end = start + size
    page_items = all_items[start:end]

    items = []
    for item in page_items:
        items.append(ReviewItem(
            key=item.get("key", ""),
            path=item.get("path", ""),
            audio_url=item.get("audio_url", ""),
            text=item.get("text", ""),
            speaker=item.get("speaker", ""),
            duration=item.get("duration", 0.0),
            flags=item.get("flags", []),
            text_edited=item.get("text_edited"),
            quality_poor=item.get("quality_poor", 0),
            paralanguage_heavy=item.get("paralanguage_heavy", 0),
            hardcode_error=item.get("hardcode_error", 0),
            too_short=item.get("too_short", 0),
        ))

    return ReviewItemsResponse(
        items=items,
        total=total,
        page=page,
        size=size,
    )


# ---------------------------------------------------------------------------
# GET /{task_id}/download/jsonl
# ---------------------------------------------------------------------------

@router.get("/{task_id}/download/jsonl")
async def download_jsonl(
    task_id: str,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream merged data.jsonl from all speaker directories."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    output_path = input_data.get("output_path", "")

    def iter_jsonl():
        if output_path and os.path.isdir(output_path):
            for root, _dirs, files in os.walk(output_path):
                for fname in files:
                    if fname == "data.jsonl":
                        file_path = os.path.join(root, fname)
                        with open(file_path, "r", encoding="utf-8") as f:
                            for line in f:
                                yield line
                            yield "\n"

    return StreamingResponse(
        iter_jsonl(),
        media_type="application/jsonlines",
        headers={"Content-Disposition": 'attachment; filename="data.jsonl"'},
    )


# ---------------------------------------------------------------------------
# GET /{task_id}/download/special
# ---------------------------------------------------------------------------

@router.get("/{task_id}/download/special")
async def download_special_list(
    task_id: str,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream JSON list of files in _special/ with their categories."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    output_path = input_data.get("output_path", "")
    special_dir = os.path.join(output_path, "_special") if output_path else ""

    def iter_special():
        if special_dir and os.path.isdir(special_dir):
            result = []
            for entry in sorted(os.listdir(special_dir)):
                entry_path = os.path.join(special_dir, entry)
                if os.path.isdir(entry_path):
                    files_in_cat = []
                    for fname in sorted(os.listdir(entry_path)):
                        fpath = os.path.join(entry_path, fname)
                        if os.path.isfile(fpath):
                            files_in_cat.append({
                                "name": fname,
                                "path": fpath,
                                "size": os.path.getsize(fpath),
                            })
                    result.append({
                        "category": entry,
                        "files": files_in_cat,
                    })
            yield json.dumps(result, ensure_ascii=False, indent=2)
        else:
            yield json.dumps([], ensure_ascii=False)

    return StreamingResponse(
        iter_special(),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="special_files.json"'},
    )


# ---------------------------------------------------------------------------
# GET /{task_id}/download/errors
# ---------------------------------------------------------------------------

@router.get("/{task_id}/download/errors")
async def download_hardcode_errors(
    task_id: str,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream JSONL of items flagged hardcode_error=1 for manual reprocessing."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    review_items = input_data.get("review_items") or []

    def iter_errors():
        for item in review_items:
            if item.get("hardcode_error") == 1:
                yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(
        iter_errors(),
        media_type="application/jsonlines",
        headers={"Content-Disposition": 'attachment; filename="hardcode_errors.jsonl"'},
    )


# ---------------------------------------------------------------------------
# GET /{task_id}/browse  — lazy-load directory listing for file browser
# ---------------------------------------------------------------------------

@router.get("/{task_id}/browse")
async def browse_directory(
    task_id: str,
    path: str = Query(default="", description="Relative path under output_path"),
    db: Session = Depends(get_db),
):
    """List directory contents for the file browser.
    Returns {dirs: [{name, path}], files: [{name, path, size_bytes, is_audio}]}
    path is relative to output_path. Lazy-load: only loads one level at a time."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    output_path = input_data.get("output_path", "")
    if not output_path:
        raise HTTPException(status_code=400, detail="output_path not set")

    from backend.tools.classification import AUDIO_EXTENSIONS

    # Security: resolve and check bounds
    base = Path(output_path).resolve()
    if path:
        target = (base / path).resolve()
        if not str(target).startswith(str(base)):
            raise HTTPException(status_code=403, detail="Path out of bounds")
    else:
        target = base

    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Path not found or not a directory")

    dirs = []
    files = []
    try:
        for entry in sorted(target.iterdir()):
            rel = str(entry.relative_to(base))
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                # Count audio files (just direct children for speed)
                audio_count = sum(
                    1 for f in entry.iterdir()
                    if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                )
                dirs.append({"name": entry.name, "path": rel, "audio_count": audio_count})
            elif entry.is_file():
                is_audio = entry.suffix.lower() in AUDIO_EXTENSIONS
                files.append({
                    "name": entry.name,
                    "path": rel,
                    "size_bytes": entry.stat().st_size,
                    "is_audio": is_audio,
                    "ext": entry.suffix.lower(),
                })
    except PermissionError:
        pass

    return {"dirs": dirs, "files": files, "current_path": path}


# ---------------------------------------------------------------------------
# GET /{task_id}/output-audio  — serve audio files from output_path
# ---------------------------------------------------------------------------

@router.get("/{task_id}/output-audio")
async def serve_output_audio(
    task_id: str,
    path: str = Query(..., description="Path relative to task output_path"),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve an audio file from the task's output directory.
    Used for review-items playback and dataset browser preview."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    output_path = input_data.get("output_path", "")
    if not output_path:
        raise HTTPException(status_code=400, detail="output_path not set for this task")

    base = Path(output_path).resolve()
    requested = (base / path).resolve()

    if not str(requested).startswith(str(base)):
        raise HTTPException(status_code=403, detail="Path outside output directory")

    if not requested.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")

    suffix = requested.suffix.lower()
    media_type = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
    }.get(suffix, "application/octet-stream")

    return FileResponse(str(requested), media_type=media_type)


# ---------------------------------------------------------------------------
# GET /{task_id}/audio  — serve preview cache audio files (before/after)
# ---------------------------------------------------------------------------

@router.get("/{task_id}/audio")
async def serve_audio(
    task_id: str,
    path: str = Query(..., description="Relative path under STORAGE_DIR"),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve a preview-cache audio file.
    Only allows reading files under storage/ingest_sessions/{task_id}/.
    path param must be relative (e.g. ingest_sessions/{task_id}/preview_cache/...)."""
    # Security: verify task exists
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    # Resolve and bounds-check: only allow reads under storage/ingest_sessions/{task_id}/
    allowed_root = (STORAGE_DIR / "ingest_sessions" / task_id).resolve()
    requested = (STORAGE_DIR / path).resolve()

    if not str(requested).startswith(str(allowed_root)):
        raise HTTPException(status_code=403, detail="Path outside allowed directory")

    if not requested.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")

    suffix = requested.suffix.lower()
    media_type = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
    }.get(suffix, "application/octet-stream")

    return FileResponse(str(requested), media_type=media_type)


# ---------------------------------------------------------------------------
# GET /{task_id}/metadata-preview  — sample metadata.json files after generation
# ---------------------------------------------------------------------------

@router.get("/{task_id}/metadata-preview")
async def get_metadata_preview(
    task_id: str,
    limit: int = Query(default=5, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Return first `limit` speakers' metadata.json content for preview."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    output_path = input_data.get("output_path", "")
    if not output_path or not Path(output_path).is_dir():
        return {"speakers": [], "total": 0}

    results = []
    p = Path(output_path)
    for spk_dir in sorted(p.iterdir()):
        if not spk_dir.is_dir() or spk_dir.name.startswith(".") or spk_dir.name == "_special":
            continue
        meta_path = spk_dir / "metadata.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                results.append(meta)
                if len(results) >= limit:
                    break
            except Exception:
                pass

    total = sum(1 for d in p.iterdir()
                if d.is_dir() and not d.name.startswith(".") and d.name != "_special"
                and (d / "metadata.json").exists())
    return {"speakers": results, "total": total}


# ---------------------------------------------------------------------------
# GET /{task_id}/asr-preview  — sample data.jsonl rows after ASR
# ---------------------------------------------------------------------------

@router.get("/{task_id}/asr-preview")
async def get_asr_preview(
    task_id: str,
    limit: int = Query(default=15, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Return first `limit` data.jsonl rows across all speakers for preview."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    input_data = json.loads(task.input or "{}")
    output_path = input_data.get("output_path", "")
    if not output_path or not Path(output_path).is_dir():
        return {"rows": [], "total_rows": 0, "total_speakers": 0}

    rows = []
    total_rows = 0
    total_speakers = 0
    p = Path(output_path)

    for spk_dir in sorted(p.iterdir()):
        if not spk_dir.is_dir() or spk_dir.name.startswith(".") or spk_dir.name == "_special":
            continue
        jsonl_path = spk_dir / "data.jsonl"
        if not jsonl_path.is_file():
            continue
        total_speakers += 1
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    total_rows += 1
                    if len(rows) < limit:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass

    return {"rows": rows, "total_rows": total_rows, "total_speakers": total_speakers}
