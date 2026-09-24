import json
import uuid
import os
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.task import Task, File as FileModel, ModelRegistry, BenchmarkRun
from backend.schemas.task import (
    TTSRunRequest, TTSRunResponse,
    ModelsListOut, ModelInfo,
    AnalyzeJsonlRequest, AnalyzeJsonlResponse, PromptInfo,
    BenchmarkRunRequest, BenchmarkRunResponse,
    BenchmarkRunOut, BenchmarkTaskSummary,
    BenchmarkRunListItem, BenchmarkRunListOut,
)

router = APIRouter()


@router.get("/models", response_model=ModelsListOut)
def list_models(db: Session = Depends(get_db)):
    rows = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.status == "active")
        .order_by(ModelRegistry.sort_order)
        .all()
    )
    models = []
    for r in rows:
        schema = json.loads(r.params_schema or "{}")
        models.append(ModelInfo(
            id=r.id,
            display_name=r.display_name,
            status=r.status,
            model_type=r.model_type,
            params_schema=schema,
        ))
    return ModelsListOut(models=models)


@router.post("/analyze-jsonl", response_model=AnalyzeJsonlResponse)
def analyze_jsonl(req: AnalyzeJsonlRequest, db: Session = Depends(get_db)):
    f = db.get(FileModel, req.jsonl_file_id)
    if not f:
        raise HTTPException(status_code=404, detail="JSONL file not found")

    counts: dict[str, int] = defaultdict(int)
    paths: dict[str, str] = {}
    total = 0
    empty_count = 0
    with open(f.stored_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            sp = item.get("source_path", "")
            if sp:
                bn = os.path.basename(sp)
                paths[bn] = sp
                counts[bn] += 1
            else:
                empty_count += 1

    prompts = [
        PromptInfo(
            basename=bn,
            source_path=sp,
            exists_on_server=os.path.isfile(sp),
            item_count=counts[bn],
        )
        for bn, sp in paths.items()
    ]
    missing = [p.basename for p in prompts if not p.exists_on_server]
    return AnalyzeJsonlResponse(
        total_items=total,
        prompts=prompts,
        missing_prompts=missing,
        has_any_prompt=len(paths) > 0,
        empty_source_path_count=empty_count,
    )


@router.post("/run", response_model=TTSRunResponse)
def run_tts(req: TTSRunRequest, db: Session = Depends(get_db)):
    jsonl_file = db.get(FileModel, req.jsonl_file_id)
    if not jsonl_file:
        raise HTTPException(status_code=404, detail="JSONL file not found")

    model = db.get(ModelRegistry, req.model_id)
    if not model or model.status != "active":
        raise HTTPException(status_code=400, detail=f"模型 {req.model_id} 不可用")

    prompt_path = None
    if req.prompt_audio_file_id:
        pf = db.get(FileModel, req.prompt_audio_file_id)
        if not pf:
            raise HTTPException(status_code=404, detail="Prompt audio file not found")
        prompt_path = pf.stored_path

    # 解析 prompt_map: basename -> file_id → 实际路径
    resolved_map: dict[str, str] = {}
    for basename, file_id in req.prompt_map.items():
        mf = db.get(FileModel, file_id)
        if not mf:
            raise HTTPException(status_code=404, detail=f"Prompt file not found: {basename}")
        resolved_map[basename] = mf.stored_path

    task_input = {
        "jsonl_path": jsonl_file.stored_path,
        "global_prompt_path": prompt_path,
        "prompt_map": resolved_map,
        "model_id": req.model_id,
        "params": req.params,
        "benchmark_run_id": None,
    }

    task = Task(
        id=str(uuid.uuid4()),
        type="tts",
        input=json.dumps(task_input, ensure_ascii=False),
    )
    db.add(task)
    db.commit()
    return TTSRunResponse(task_id=task.id)


@router.post("/benchmark", response_model=BenchmarkRunResponse)
def run_benchmark(req: BenchmarkRunRequest, db: Session = Depends(get_db)):
    if not req.model_ids:
        raise HTTPException(status_code=400, detail="model_ids 不能为空")

    jsonl_file = db.get(FileModel, req.jsonl_file_id)
    if not jsonl_file:
        raise HTTPException(status_code=404, detail="JSONL file not found")

    prompt_path = None
    if req.prompt_audio_file_id:
        pf = db.get(FileModel, req.prompt_audio_file_id)
        if not pf:
            raise HTTPException(status_code=404, detail="Prompt audio file not found")
        prompt_path = pf.stored_path

    resolved_map: dict[str, str] = {}
    for basename, file_id in req.prompt_map.items():
        mf = db.get(FileModel, file_id)
        if not mf:
            raise HTTPException(status_code=404, detail=f"Prompt file not found: {basename}")
        resolved_map[basename] = mf.stored_path

    for mid in req.model_ids:
        m = db.get(ModelRegistry, mid)
        if not m or m.status != "active":
            raise HTTPException(status_code=400, detail=f"模型 {mid} 不可用")

    run_id = str(uuid.uuid4())
    benchmark = BenchmarkRun(
        id=run_id,
        name=req.name,
        jsonl_path=jsonl_file.stored_path,
        prompt_audio_path=prompt_path,
        prompt_map=json.dumps(resolved_map),
        model_ids=json.dumps(req.model_ids),
        status="pending",
    )
    db.add(benchmark)

    task_ids = []
    for mid in req.model_ids:
        params = req.params_per_model.get(mid, {})
        task_input = {
            "jsonl_path": jsonl_file.stored_path,
            "global_prompt_path": prompt_path,
            "prompt_map": resolved_map,
            "model_id": mid,
            "params": params,
            "benchmark_run_id": run_id,
        }
        task = Task(
            id=str(uuid.uuid4()),
            type="tts",
            input=json.dumps(task_input, ensure_ascii=False),
        )
        db.add(task)
        task_ids.append(task.id)

    db.commit()
    return BenchmarkRunResponse(benchmark_run_id=run_id, task_ids=task_ids, status="pending")


@router.get("/benchmark/{run_id}", response_model=BenchmarkRunOut)
def get_benchmark(run_id: str, db: Session = Depends(get_db)):
    run = db.get(BenchmarkRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="BenchmarkRun not found")

    tasks = db.query(Task).filter(Task.input.contains(run_id)).all()
    task_summaries = []
    for t in tasks:
        inp = json.loads(t.input or "{}")
        result = json.loads(t.result or "{}") if t.result else {}
        task_summaries.append(BenchmarkTaskSummary(
            task_id=t.id,
            model_id=inp.get("model_id", ""),
            status=t.status,
            total=result.get("total"),
            ok=result.get("ok"),
        ))

    return BenchmarkRunOut(
        id=run.id,
        name=run.name,
        status=run.status,
        tasks=task_summaries,
    )


@router.get("/benchmarks", response_model=BenchmarkRunListOut)
def list_benchmarks(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    total = db.query(BenchmarkRun).count()
    runs = (
        db.query(BenchmarkRun)
        .order_by(BenchmarkRun.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = []
    for r in runs:
        model_ids = json.loads(r.model_ids or "[]")
        task_count = db.query(Task).filter(Task.input.contains(r.id)).count()
        items.append(BenchmarkRunListItem(
            id=r.id,
            name=r.name,
            status=r.status,
            model_ids=model_ids,
            created_at=r.created_at,
            finished_at=r.finished_at,
            task_count=task_count,
        ))
    return BenchmarkRunListOut(items=items, total=total)
