import json
import asyncio
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.task import Task, File, ModelRegistry, PromptExperiment, SynthesisRecord
from backend.schemas.task import (
    TaskOut,
    PromptExperimentRequest,
    ConfirmAsrRequest,
    ConfirmTextsRequest,
    ConfirmPlanRequest,
    ReviewSubmission,
    ExperimentHistoryItem,
    ExperimentHistoryResponse,
    PromptExperimentV3Request,
    ConfirmAsrV3Request,
    ConfirmPlanV3Request,
    ConfirmRoundRequest,
    SubmitEvaluationRequest,
)

router = APIRouter()

from backend.config import OUTPUTS_DIR as OUTPUTS_BASE


# ── 通用 Agent 任务端点 ────────────────────────────────────────────────────────

@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_agent_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskOut.from_orm_task(task)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, db: Session = Depends(get_db)):
    """通用终止：将 pending/running/awaiting_review 任务标记为 failed"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status in ("success", "failed"):
        raise HTTPException(status_code=400, detail="任务已结束")
    task.status = "failed"
    task.error = "用户手动终止"
    task.finished_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.post("/tasks/{task_id}/resume")
def resume_agent_task(task_id: str, db: Session = Depends(get_db)):
    """不带人工数据的强制恢复（应急用），带数据的操作走各专属端点。"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "awaiting_review":
        raise HTTPException(
            status_code=400,
            detail=f"Task is not awaiting review (status={task.status})",
        )
    task.status = "pending"
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task_id}


# ── Agent 1: 克隆实验室 ───────────────────────────────────────────────────────

@router.post("/prompt-experiment")
def create_prompt_experiment(body: PromptExperimentRequest, db: Session = Depends(get_db)):
    """创建一次克隆实验任务。"""
    # 解析 file_id → stored_path
    prompts = []
    for fid in body.prompt_file_ids:
        f = db.get(File, fid)
        if not f:
            raise HTTPException(status_code=404, detail=f"File {fid} not found")
        prompts.append({
            "file_id": fid,
            "stored_path": f.stored_path,
            "asr_text": body.prompt_texts.get(fid, ""),
        })

    ctx = body.business_context
    context_key = "_".join([
        ctx.get("character_type", ""),
        ctx.get("emotion_register", ""),
        ctx.get("content_type", ""),
        ctx.get("language_style", ""),
        ctx.get("special_req", "无"),
    ])

    input_data = {
        "phase": "start",
        "prompts": prompts,
        "business_context": ctx,
        "context_key": context_key,
        "model_ids": body.model_ids,
        "params_map": body.params_map,
        "test_text_count": body.test_text_count,
        "synthesis_scene": body.synthesis_scene,
    }
    task = Task(
        type="prompt_experiment",
        status="pending",
        input=json.dumps(input_data, ensure_ascii=False),
    )
    db.add(task)
    db.commit()
    return {"task_id": task.id}


# ⚠️ history 路由必须在 /{task_id} 之前注册
@router.get("/prompt-experiment/history", response_model=ExperimentHistoryResponse)
def get_experiment_history(context_key: str | None = None, db: Session = Depends(get_db)):
    """查询历史实验结果（按 context_key 过滤，按 combo+model 聚合）。"""
    q = db.query(
        PromptExperiment.business_context_key,
        PromptExperiment.prompt_combo,
        PromptExperiment.model_id,
        func.avg(PromptExperiment.human_score).label("avg_score"),
        func.count(PromptExperiment.id).label("n_samples"),
    )
    if context_key:
        q = q.filter(PromptExperiment.business_context_key == context_key)
    rows = q.group_by(
        PromptExperiment.business_context_key,
        PromptExperiment.prompt_combo,
        PromptExperiment.model_id,
    ).all()

    items = [
        ExperimentHistoryItem(
            business_context_key=r.business_context_key,
            prompt_combo=r.prompt_combo,
            model_id=r.model_id,
            avg_score=round(r.avg_score, 2) if r.avg_score is not None else None,
            n_samples=r.n_samples,
        )
        for r in rows
    ]
    return ExperimentHistoryResponse(items=items, total=len(items))


@router.get("/prompt-experiment/{task_id}", response_model=TaskOut)
def get_prompt_experiment(task_id: str, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.type != "prompt_experiment":
        raise HTTPException(status_code=404, detail="Experiment task not found")
    return TaskOut.from_orm_task(task)


@router.post("/prompt-experiment/{task_id}/confirm-asr")
def confirm_asr(task_id: str, body: ConfirmAsrRequest, db: Session = Depends(get_db)):
    """提交用户校对后的 ASR 文本，恢复任务执行。"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"Task status={task.status}, expected awaiting_review")
    input_data = json.loads(task.input or "{}")
    if input_data.get("phase") != "confirm_asr":
        raise HTTPException(status_code=400, detail=f"Wrong phase: {input_data.get('phase')}")
    prompts = input_data.get("prompts", [])
    for i, text in enumerate(body.asr_texts):
        if i < len(prompts):
            prompts[i]["asr_text"] = text
    input_data["prompts"] = prompts
    task.input = json.dumps(input_data, ensure_ascii=False)
    task.status = "pending"
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task_id}


@router.post("/prompt-experiment/{task_id}/confirm-texts")
def confirm_texts(task_id: str, body: ConfirmTextsRequest, db: Session = Depends(get_db)):
    """提交人工确认/编辑后的测试文本，恢复任务执行。"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"Task status={task.status}, expected awaiting_review")
    input_data = json.loads(task.input or "{}")
    if input_data.get("phase") != "confirm_texts":
        raise HTTPException(status_code=400, detail=f"Wrong phase: {input_data.get('phase')}")
    if not body.confirmed_texts:
        raise HTTPException(status_code=422, detail="confirmed_texts cannot be empty")
    input_data["confirmed_texts"] = body.confirmed_texts
    task.input = json.dumps(input_data, ensure_ascii=False)
    task.status = "pending"
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task_id}


@router.post("/prompt-experiment/{task_id}/confirm-plan")
def confirm_plan(task_id: str, body: ConfirmPlanRequest, db: Session = Depends(get_db)):
    """提交人工确认/剪裁后的实验规划，恢复任务执行。"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"Task status={task.status}, expected awaiting_review")
    input_data = json.loads(task.input or "{}")
    if input_data.get("phase") != "confirm_plan":
        raise HTTPException(status_code=400, detail=f"Wrong phase: {input_data.get('phase')}")
    if not body.confirmed_plan:
        raise HTTPException(status_code=422, detail="confirmed_plan cannot be empty")
    input_data["confirmed_plan"] = body.confirmed_plan
    task.input = json.dumps(input_data, ensure_ascii=False)
    task.status = "pending"
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task_id}


@router.post("/prompt-experiment/{task_id}/review")
def submit_review(task_id: str, body: ReviewSubmission, db: Session = Depends(get_db)):
    """提交人工评分和 winner 选择，恢复任务执行。"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"Task status={task.status}, expected awaiting_review")
    input_data = json.loads(task.input or "{}")
    if input_data.get("phase") != "select_winner":
        raise HTTPException(status_code=400, detail=f"Wrong phase: {input_data.get('phase')}")
    input_data["human_selections"] = body.human_selections
    input_data["evaluations"] = body.evaluations
    task.input = json.dumps(input_data, ensure_ascii=False)
    task.status = "pending"
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task_id}


@router.get("/prompt-experiment/{task_id}/export")
def export_experiment(task_id: str, db: Session = Depends(get_db)):
    """下载 experiment_result.jsonl。"""
    task = db.get(Task, task_id)
    if not task or task.type != "prompt_experiment":
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "success":
        raise HTTPException(status_code=400, detail="Experiment not yet complete")
    result = json.loads(task.result or "{}")
    jsonl_path = result.get("jsonl_path", "")
    if not jsonl_path or not os.path.isfile(jsonl_path):
        raise HTTPException(status_code=404, detail="Result file not found")
    return FileResponse(
        jsonl_path,
        media_type="application/jsonlines",
        filename=f"experiment_{task_id}.jsonl",
    )


# ── Agent 2: 数据处理 ─────────────────────────────────────────────────────────
# (端点在 Phase 2 实现)


# ── Agent 3: 评测中心 ─────────────────────────────────────────────────────────
# (端点在 Phase 3 实现)


# ── Agent 4: 部署模型 ─────────────────────────────────────────────────────────
# (端点在 Phase 4 实现)


# ══════════════════════════════════════════════════════════════════════════════
# Agent 1 v3: 克隆实验室（agent-native 多轮迭代）
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/prompt-experiment-v3")
def create_prompt_experiment_v3(body: PromptExperimentV3Request, db: Session = Depends(get_db)):
    """创建 v3 克隆实验任务。"""
    prompts = []
    for fid in body.prompt_file_ids:
        f = db.get(File, fid)
        if not f:
            raise HTTPException(status_code=404, detail=f"File {fid} not found")
        entry = {
            "file_id": fid,
            "stored_path": f.stored_path,
            "asr_text": "",
        }
        if fid in body.prompt_annotations:
            entry["annotation"] = body.prompt_annotations[fid]
        prompts.append(entry)

    # 解析 user_texts（手动输入或 JSONL 文件）
    user_texts = body.user_texts or []
    if not user_texts and body.user_texts_file_id:
        f = db.get(File, body.user_texts_file_id)
        if f and os.path.isfile(f.stored_path):
            with open(f.stored_path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        user_texts.append(obj.get("text", obj.get("content", line)))
                    except json.JSONDecodeError:
                        user_texts.append(line)

    input_data = {
        "phase": "start",
        "prompts": prompts,
        "scene_type": body.scene_type,
        "scene_description": body.scene_description,
        "scene_form": body.scene_form,
        "model_ids": body.model_ids,
        "params_map": body.params_map,
        "user_texts": user_texts,
        "max_synthesis_per_round": body.max_synthesis_per_round or 30,
        "auto_text_count": body.auto_text_count,
        "round": 0,
        "task_history": [],
        "hypotheses": [],
        "agent_events": [],
    }
    task = Task(
        type="prompt_experiment_v3",
        status="pending",
        input=json.dumps(input_data, ensure_ascii=False),
    )
    db.add(task)
    db.commit()
    return {"task_id": task.id}


@router.get("/prompt-experiment-v3/history")
def get_experiment_v3_history(scene_type: str | None = None, db: Session = Depends(get_db)):
    """v3 历史任务列表"""
    q = db.query(Task).filter(Task.type == "prompt_experiment_v3")
    if scene_type:
        q = q.filter(Task.input.contains(scene_type))
    tasks = q.order_by(Task.created_at.desc()).limit(50).all()
    return {
        "items": [TaskOut.from_orm_task(t) for t in tasks],
        "total": len(tasks),
    }


@router.get("/prompt-experiment-v3/{task_id}", response_model=TaskOut)
def get_prompt_experiment_v3(task_id: str, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.type != "prompt_experiment_v3":
        raise HTTPException(status_code=404, detail="V3 experiment task not found")
    return TaskOut.from_orm_task(task)


@router.post("/prompt-experiment-v3/{task_id}/cancel")
def cancel_experiment_v3(task_id: str, db: Session = Depends(get_db)):
    """终止任务（pending/running/awaiting_review → failed）"""
    task = db.get(Task, task_id)
    if not task or task.type != "prompt_experiment_v3":
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status in ("success", "failed"):
        raise HTTPException(status_code=400, detail="任务已结束，无法终止")
    task.status = "failed"
    task.error = "用户手动终止"
    task.finished_at = datetime.utcnow()
    # 同时终止其 running 的子任务
    sub_tasks = db.query(Task).filter(
        Task.type == "tts",
        Task.status.in_(["pending", "running"]),
        Task.input.contains(task_id),
    ).all()
    for st in sub_tasks:
        st.status = "failed"
        st.error = "父任务已终止"
        st.finished_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "cancelled_sub_tasks": len(sub_tasks)}


@router.get("/prompt-experiment-v3/{task_id}/stream")
async def stream_experiment_v3(task_id: str, db: Session = Depends(get_db)):
    """SSE endpoint: 实时推送 agent_events 增量和 status 变化"""

    async def event_generator():
        last_event_count = 0
        last_status = ""
        last_phase = ""
        last_streaming = None
        last_synth_done = -1
        last_sub_task_id = None
        last_sub_log_count = 0
        last_task_log_count = 0
        while True:
            db.expire_all()
            task = db.get(Task, task_id)
            if not task:
                yield {"event": "error", "data": "task not found"}
                break
            input_data = json.loads(task.input or "{}")
            status = task.status
            phase = input_data.get("phase", "")

            if status != last_status or phase != last_phase:
                last_status = status
                last_phase = phase
                yield {"event": "state", "data": json.dumps({"status": status, "phase": phase})}

            events = input_data.get("agent_events", [])
            if len(events) > last_event_count:
                new_events = events[last_event_count:]
                last_event_count = len(events)
                yield {"event": "events", "data": json.dumps(new_events, ensure_ascii=False)}

            # Send streaming partial (thinking/text in progress)
            streaming = input_data.get("_streaming")
            if streaming != last_streaming:
                last_streaming = streaming
                if streaming:
                    yield {"event": "streaming", "data": json.dumps(streaming, ensure_ascii=False)}
                else:
                    yield {"event": "streaming", "data": "null"}

            # Push synthesis progress updates
            synth_prog = input_data.get("synthesis_progress")
            synth_done = synth_prog.get("done", -1) if synth_prog else -1
            if synth_done != last_synth_done:
                last_synth_done = synth_done
                yield {"event": "synthesis_progress", "data": json.dumps({
                    **(synth_prog or {}),
                    "current_synth_model": input_data.get("current_synth_model", ""),
                }, ensure_ascii=False)}

            # Push synthesis logs: 父任务日志（模型级）+ 当前 sub_task 日志（条目级）
            task_logs = json.loads(task.logs or "[]")
            if len(task_logs) > last_task_log_count:
                new_logs = task_logs[last_task_log_count:]
                last_task_log_count = len(task_logs)
                yield {"event": "synthesis_log", "data": json.dumps(new_logs, ensure_ascii=False)}

            sub_task_id = input_data.get("current_sub_task_id")
            if sub_task_id:
                if sub_task_id != last_sub_task_id:
                    last_sub_task_id = sub_task_id
                    last_sub_log_count = 0
                sub_task = db.get(Task, sub_task_id)
                if sub_task:
                    sub_logs = json.loads(sub_task.logs or "[]")
                    if len(sub_logs) > last_sub_log_count:
                        new_logs = sub_logs[last_sub_log_count:]
                        last_sub_log_count = len(sub_logs)
                        yield {"event": "synthesis_log", "data": json.dumps(new_logs, ensure_ascii=False)}
            else:
                last_sub_task_id = None
                last_sub_log_count = 0

            if status in ("success", "failed"):
                yield {"event": "done", "data": json.dumps({"status": status, "phase": phase})}
                break

            # awaiting_review: 保持 SSE 连接，降低轮询频率等待用户评价完成
            if status == "awaiting_review":
                await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(0.2)

    return EventSourceResponse(event_generator())


@router.post("/prompt-experiment-v3/{task_id}/confirm-asr")
def confirm_asr_v3(task_id: str, body: ConfirmAsrV3Request, db: Session = Depends(get_db)):
    """提交 prompt ASR 校对 + 标注问卷"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"Task status={task.status}, expected awaiting_review")
    input_data = json.loads(task.input or "{}")
    if input_data.get("phase") != "confirm_asr":
        raise HTTPException(status_code=400, detail=f"Wrong phase: {input_data.get('phase')}")

    prompts = input_data.get("prompts", [])
    for item in body.prompts:
        for i, p in enumerate(prompts):
            if p.get("file_id") == item.get("file_id"):
                prompts[i]["asr_text"] = item.get("asr_text", p.get("asr_text", ""))
                if item.get("annotation"):
                    prompts[i]["annotation"] = item["annotation"]
                break

    input_data["prompts"] = prompts
    task.input = json.dumps(input_data, ensure_ascii=False)
    task.status = "pending"
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task_id}


@router.post("/prompt-experiment-v3/{task_id}/confirm-plan")
def confirm_plan_v3(task_id: str, body: ConfirmPlanV3Request, db: Session = Depends(get_db)):
    """确认任务探索计划"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"Task status={task.status}, expected awaiting_review")
    input_data = json.loads(task.input or "{}")
    if input_data.get("phase") != "confirm_plan":
        raise HTTPException(status_code=400, detail=f"Wrong phase: {input_data.get('phase')}")

    if body.user_direction:
        input_data["user_direction"] = body.user_direction
    task.input = json.dumps(input_data, ensure_ascii=False)
    task.status = "pending"
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task_id}


@router.post("/prompt-experiment-v3/{task_id}/confirm-round")
def confirm_round_v3(task_id: str, body: ConfirmRoundRequest, db: Session = Depends(get_db)):
    """确认本轮合成组合"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"Task status={task.status}, expected awaiting_review")
    input_data = json.loads(task.input or "{}")
    if input_data.get("phase") != "confirm_round":
        raise HTTPException(status_code=400, detail=f"Wrong phase: {input_data.get('phase')}")

    if not body.confirmed_combo_ids:
        raise HTTPException(status_code=422, detail="confirmed_combo_ids cannot be empty")
    input_data["confirmed_combo_ids"] = body.confirmed_combo_ids
    task.input = json.dumps(input_data, ensure_ascii=False)
    task.status = "pending"
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task_id}


@router.post("/prompt-experiment-v3/{task_id}/submit-evaluation")
def submit_evaluation_v3(task_id: str, body: SubmitEvaluationRequest, db: Session = Depends(get_db)):
    """提交排名评价"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"Task status={task.status}, expected awaiting_review")
    input_data = json.loads(task.input or "{}")
    phase = input_data.get("phase", "")
    if phase not in ("evaluation", "awaiting_review"):
        raise HTTPException(status_code=400, detail=f"Wrong phase: {phase}")

    input_data["pending_evaluation"] = {
        "round": body.round,
        "rankings_by_text": body.rankings_by_text,
        "per_combo": body.per_combo,
        "winner": body.winner,
        "next_round_direction": body.next_round_direction,
        "finish_requested": body.finish_requested,
    }
    task.input = json.dumps(input_data, ensure_ascii=False)
    task.status = "pending"
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task_id}


@router.get("/prompt-experiment-v3/{task_id}/winner-config")
def get_winner_config(task_id: str, db: Session = Depends(get_db)):
    """获取 winner 完整配置（供批量合成页预填）— legacy"""
    task = db.get(Task, task_id)
    if not task or task.type != "prompt_experiment_v3":
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "success":
        raise HTTPException(status_code=400, detail="Experiment not yet complete")
    result = json.loads(task.result or "{}")
    return result.get("winner_config", {})


@router.get("/prompt-experiment-v3/{task_id}/portfolio")
def get_portfolio(task_id: str, db: Session = Depends(get_db)):
    """获取策略档案（agent loop 模式）"""
    task = db.get(Task, task_id)
    if not task or task.type != "prompt_experiment_v3":
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "success":
        raise HTTPException(status_code=400, detail="Experiment not yet complete")
    result = json.loads(task.result or "{}")
    return {
        "portfolio": result.get("portfolio", []),
        "summary": result.get("summary", ""),
        "total_rounds": result.get("total_rounds", 0),
        "reason": result.get("reason", ""),
    }


@router.get("/prompt-experiment-v3/kb/coverage")
def get_kb_coverage(scene_type: str | None = None, db: Session = Depends(get_db)):
    """获取覆盖矩阵"""
    from backend.tools.kb_tools import KB_ROOT
    matrix_path = KB_ROOT / "coverage" / "model_scene_matrix.md"
    if not matrix_path.exists():
        return {"content": "", "message": "覆盖矩阵尚未建立"}
    return {"content": matrix_path.read_text(encoding="utf-8")}


@router.post("/prompt-experiment-v3/aggregate")
def trigger_aggregation(db: Session = Depends(get_db)):
    """手动触发 KB 聚合"""
    from backend.models.task import Task as TaskModel
    agg_task = TaskModel(
        type="kb_aggregation",
        status="pending",
        input=json.dumps({"trigger": "manual"}, ensure_ascii=False),
    )
    db.add(agg_task)
    db.commit()
    return {"ok": True, "task_id": agg_task.id}
