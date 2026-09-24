"""离线主观评测 API。

流程：创建任务（上传ZIP+JSONL）→ 按text分组 → 用户逐组评分 → 汇总统计。
并发安全：GET /next-group 使用乐观锁（checked_out_at），5分钟无提交视为超时放弃。
"""
import json
import zipfile
import io
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.config import STORAGE_DIR
from backend.database import get_db
from backend.models.offline_eval import EvalTask, EvalGroup, EvalSample, EvalRating

router = APIRouter()

EVAL_AUDIO_DIR = STORAGE_DIR / "offline_eval"


# ── Schemas ──────────────────────────────────────────────────────────────────

class SampleRating(BaseModel):
    sample_id: str
    mos_score: Optional[float] = None
    nmos_score: Optional[float] = None
    smos_score: Optional[float] = None
    issue_tags: list[str] = []
    notes: str = ""


class SubmitRatingsRequest(BaseModel):
    session_id: str
    ratings: list[SampleRating]


class GroupSubmitItem(BaseModel):
    group_id: str
    ratings: list[SampleRating]


class BatchSubmitRequest(BaseModel):
    session_id: str
    groups: list[GroupSubmitItem]


class ReleaseCheckoutRequest(BaseModel):
    session_id: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def _audio_url(rel_path: str) -> str:
    """Convert relative storage path to URL served by /storage mount."""
    return f"/storage/{rel_path.lstrip('/')}"


def _group_to_dict(group: EvalGroup, samples: list[EvalSample], metrics: list[str]) -> dict:
    show_smos = "smos" in metrics
    return {
        "id": group.id,
        "text": group.text,
        "eval_count": group.eval_count,
        "samples": [
            {
                "id": s.id,
                "key": s.key,
                "model": s.model,
                "speaker": s.speaker,
                "speaker_type": s.speaker_type,
                "test_set": s.test_set,
                "audio_url": _audio_url(s.audio_path) if s.audio_path else None,
                "ref_audio_url": _audio_url(s.ref_audio_path) if (s.ref_audio_path and show_smos) else None,
                "wer_score": s.wer_score,
                "sim_score": s.sim_score,
            }
            for s in sorted(samples, key=lambda x: x.model)
        ],
        "metrics": metrics,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/tasks")
async def create_eval_task(
    name: str = Form(...),
    description: str = Form(""),
    metrics: str = Form('["mos"]'),
    wer_model: Optional[str] = Form(None),
    sim_model: Optional[str] = Form(None),
    jsonl_file: UploadFile = File(...),
    audio_zip: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """发布新评测任务：解析JSONL + 解压音频ZIP + 按text分组建库。"""
    import uuid
    task_id = str(uuid.uuid4())

    # 解压音频 ZIP 到 storage/offline_eval/{task_id}/audio/
    audio_dir = EVAL_AUDIO_DIR / task_id / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    zip_bytes = await audio_zip.read()
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(audio_dir)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="音频文件不是有效的ZIP格式")

    # 存储 JSONL 文件
    jsonl_dir = EVAL_AUDIO_DIR / task_id
    jsonl_path = jsonl_dir / "samples.jsonl"
    jsonl_bytes = await jsonl_file.read()
    jsonl_path.write_bytes(jsonl_bytes)

    # 解析 JSONL
    records = []
    for line in jsonl_bytes.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    if not records:
        raise HTTPException(status_code=400, detail="JSONL文件为空或格式错误")

    def _resolve_audio(raw: str) -> str | None:
        """将 JSONL 中的路径（相对于 audio_dir）解析为相对 STORAGE_DIR 的存储路径。"""
        if not raw:
            return None
        p = Path(raw) if Path(raw).is_absolute() else audio_dir / raw
        if p.exists():
            try:
                return str(p.relative_to(STORAGE_DIR))
            except ValueError:
                return raw
        return None

    def _resolve_audio_by_key(key: str) -> str | None:
        """旧格式：按 key 名称在 audio_dir 内查找音频文件。"""
        for ext in (".wav", ".mp3", ".ogg", ".flac"):
            p = audio_dir / f"{key}{ext}"
            if p.exists():
                return str(p.relative_to(STORAGE_DIR))
        for ext in (".wav", ".mp3", ".ogg", ".flac"):
            found = list(audio_dir.rglob(f"{key}{ext}"))
            if found:
                return str(found[0].relative_to(STORAGE_DIR))
        return None

    # 检测格式：新格式每行含 results 数组，旧格式每行一个样本
    is_new_format = any("results" in r for r in records)

    # 统一构建 groups：text → {meta, samples_raw}
    import uuid as _uuid
    groups_ordered: list[dict] = []
    seen_texts: set[str] = set()

    if is_new_format:
        for rec in records:
            text = rec.get("text", "").strip()
            if not text or not rec.get("results"):
                continue
            if text in seen_texts:
                continue
            seen_texts.add(text)
            groups_ordered.append({
                "text": text,
                "speaker": rec.get("speaker", ""),
                "speaker_type": rec.get("speaker_type", ""),
                "age": rec.get("age", "") or "",
                "gender": rec.get("gender", "") or "",
                "test_set": rec.get("test_set", ""),
                "ref_audio_raw": rec.get("ref_audio", ""),
                "samples": [
                    {
                        "key": f"{rec.get('key', '')}_{r.get('model', '')}",
                        "model": r.get("model", ""),
                        "audio_raw": r.get("audio_path", ""),
                    }
                    for r in rec.get("results", [])
                ],
            })
    else:
        # 旧格式：按 text 分组
        old_map: dict[str, list[dict]] = {}
        for rec in records:
            text = rec.get("text", "").strip()
            if text:
                old_map.setdefault(text, []).append(rec)
        for text, recs in old_map.items():
            first = recs[0]
            groups_ordered.append({
                "text": text,
                "speaker": first.get("speaker", ""),
                "speaker_type": first.get("speaker_type", ""),
                "age": first.get("age", "") or "",
                "gender": first.get("gender", "") or "",
                "test_set": first.get("test_set", ""),
                "ref_audio_raw": first.get("ref_audio", ""),
                "samples": [
                    {
                        "key": r.get("key", ""),
                        "model": r.get("model", ""),
                        "audio_raw": None,  # 旧格式按 key 查找
                    }
                    for r in recs
                ],
            })

    metrics_list = json.loads(metrics) if isinstance(metrics, str) else metrics
    total_samples_count = sum(len(g["samples"]) for g in groups_ordered)

    db_task = EvalTask(
        id=task_id,
        name=name,
        description=description,
        jsonl_path=str(jsonl_path),
        audio_dir=str(audio_dir),
        metrics=json.dumps(metrics_list, ensure_ascii=False),
        wer_model=wer_model,
        sim_model=sim_model,
        status="ready",
        total_groups=len(groups_ordered),
        total_samples=total_samples_count,
    )
    db.add(db_task)

    total_created = 0
    for g in groups_ordered:
        group = EvalGroup(
            id=str(_uuid.uuid4()),
            eval_task_id=task_id,
            text=g["text"],
        )
        db.add(group)
        ref_audio_path = _resolve_audio(g["ref_audio_raw"])

        for s in g["samples"]:
            if is_new_format:
                audio_path = _resolve_audio(s["audio_raw"])
            else:
                audio_path = _resolve_audio_by_key(s["key"])

            sample = EvalSample(
                eval_task_id=task_id,
                eval_group_id=group.id,
                key=s["key"],
                text=g["text"],
                speaker=g["speaker"],
                speaker_type=g["speaker_type"],
                age=g["age"],
                gender=g["gender"],
                test_set=g["test_set"],
                model=s["model"],
                audio_path=audio_path,
                ref_audio_path=ref_audio_path,
            )
            db.add(sample)
            total_created += 1

    db.commit()
    return {"task_id": task_id, "total_groups": len(groups_ordered), "total_samples": total_created}


@router.get("/tasks")
def list_eval_tasks(db: Session = Depends(get_db)):
    """列出所有评测任务，含已评价组数统计。"""
    tasks = db.query(EvalTask).order_by(EvalTask.created_at.desc()).all()
    result = []
    for t in tasks:
        evaluated = (
            db.query(func.count(EvalGroup.id))
            .filter(EvalGroup.eval_task_id == t.id, EvalGroup.eval_count > 0)
            .scalar() or 0
        )
        result.append({
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "status": t.status,
            "metrics": t.get_metrics(),
            "total_groups": t.total_groups,
            "total_samples": t.total_samples,
            "evaluated_groups": evaluated,
            "created_at": t.created_at.isoformat(),
        })
    return {"items": result}


@router.get("/tasks/{task_id}")
def get_eval_task(task_id: str, db: Session = Depends(get_db)):
    """任务详情 + 进度。"""
    t = db.get(EvalTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="评测任务不存在")
    evaluated = (
        db.query(func.count(EvalGroup.id))
        .filter(EvalGroup.eval_task_id == task_id, EvalGroup.eval_count > 0)
        .scalar() or 0
    )
    total_ratings = db.query(func.count(EvalRating.id)).filter(EvalRating.eval_task_id == task_id).scalar() or 0
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "status": t.status,
        "metrics": t.get_metrics(),
        "wer_model": t.wer_model,
        "sim_model": t.sim_model,
        "total_groups": t.total_groups,
        "total_samples": t.total_samples,
        "evaluated_groups": evaluated,
        "total_ratings": total_ratings,
        "created_at": t.created_at.isoformat(),
    }


@router.get("/tasks/{task_id}/next-group")
def get_next_group(task_id: str, exclude: str = "", db: Session = Depends(get_db)):
    """返回评价次数最少的待评价组，跳过已访问的组（exclude 为逗号分隔的 group_id）。"""
    t = db.get(EvalTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="评测任务不存在")

    exclude_ids = [x.strip() for x in exclude.split(",") if x.strip()]

    query = db.query(EvalGroup).filter(EvalGroup.eval_task_id == task_id)
    if exclude_ids:
        query = query.filter(~EvalGroup.id.in_(exclude_ids))

    group = query.order_by(EvalGroup.eval_count.asc(), EvalGroup.id.asc()).first()

    if not group:
        return {"group": None, "waiting": False, "message": "所有组已浏览，可提交评分或退出"}

    samples = db.query(EvalSample).filter(EvalSample.eval_group_id == group.id).all()
    return {"group": _group_to_dict(group, samples, t.get_metrics())}


@router.post("/tasks/{task_id}/groups/{group_id}/submit")
def submit_ratings(
    task_id: str,
    group_id: str,
    body: SubmitRatingsRequest,
    db: Session = Depends(get_db),
):
    """提交一组评分。"""
    t = db.get(EvalTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="评测任务不存在")
    group = db.get(EvalGroup, group_id)
    if not group or group.eval_task_id != task_id:
        raise HTTPException(status_code=404, detail="评价组不存在")

    for r in body.ratings:
        sample = db.get(EvalSample, r.sample_id)
        if not sample or sample.eval_task_id != task_id:
            continue
        rating = EvalRating(
            eval_task_id=task_id,
            eval_group_id=group_id,
            eval_sample_id=r.sample_id,
            session_id=body.session_id,
            mos_score=r.mos_score,
            nmos_score=r.nmos_score,
            smos_score=r.smos_score,
            issue_tags=json.dumps(r.issue_tags, ensure_ascii=False),
            notes=r.notes,
        )
        db.add(rating)

    # 增加评价计数
    group.eval_count += 1
    db.commit()
    return {"ok": True, "eval_count": group.eval_count}


@router.post("/tasks/{task_id}/submit-batch")
def submit_batch_ratings(
    task_id: str,
    body: BatchSubmitRequest,
    db: Session = Depends(get_db),
):
    """批量提交多组评分。"""
    t = db.get(EvalTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="评测任务不存在")

    submitted = 0
    for g in body.groups:
        group = db.get(EvalGroup, g.group_id)
        if not group or group.eval_task_id != task_id:
            continue

        for r in g.ratings:
            sample = db.get(EvalSample, r.sample_id)
            if not sample or sample.eval_task_id != task_id:
                continue
            rating = EvalRating(
                eval_task_id=task_id,
                eval_group_id=group.id,
                eval_sample_id=r.sample_id,
                session_id=body.session_id,
                mos_score=r.mos_score,
                nmos_score=r.nmos_score,
                smos_score=r.smos_score,
                issue_tags=json.dumps(r.issue_tags, ensure_ascii=False),
                notes=r.notes,
            )
            db.add(rating)

        group.eval_count += 1
        submitted += 1

    db.commit()
    return {"ok": True, "submitted_groups": submitted}


@router.get("/tasks/{task_id}/results")
def get_results(task_id: str, db: Session = Depends(get_db)):
    """汇总统计：各model指标均值，按测试集/音色类型分组。"""
    t = db.get(EvalTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="评测任务不存在")

    # 拉取所有评分 join 样本信息
    rows = (
        db.query(EvalRating, EvalSample)
        .join(EvalSample, EvalRating.eval_sample_id == EvalSample.id)
        .filter(EvalRating.eval_task_id == task_id)
        .all()
    )

    def _avg(vals):
        v = [x for x in vals if x is not None]
        return round(sum(v) / len(v), 3) if v else None

    def _build_stats(filtered_rows):
        mos_vals, nmos_vals, smos_vals, wer_vals, sim_vals = [], [], [], [], []
        for rating, sample in filtered_rows:
            if rating.mos_score is not None:
                mos_vals.append(rating.mos_score)
            if rating.nmos_score is not None:
                nmos_vals.append(rating.nmos_score)
            if rating.smos_score is not None:
                smos_vals.append(rating.smos_score)
            if sample.wer_score is not None:
                wer_vals.append(sample.wer_score)
            if sample.sim_score is not None:
                sim_vals.append(sample.sim_score)
        return {
            "count": len(filtered_rows),
            "avg_mos": _avg(mos_vals),
            "avg_nmos": _avg(nmos_vals),
            "avg_smos": _avg(smos_vals),
            "avg_wer": _avg(wer_vals),
            "avg_sim": _avg(sim_vals),
        }

    # 1. 总体按 model
    models: dict[str, list] = {}
    test_sets: dict[str, list] = {}
    genders: dict[str, list] = {}
    ages: dict[str, list] = {}
    gender_ages: dict[str, list] = {}

    for rating, sample in rows:
        models.setdefault(sample.model, []).append((rating, sample))
        test_sets.setdefault(sample.test_set or "未知", []).append((rating, sample))
        g = sample.gender or "未知"
        a = sample.age or "未知"
        genders.setdefault(g, []).append((rating, sample))
        ages.setdefault(a, []).append((rating, sample))
        gender_ages.setdefault(f"{g} / {a}", []).append((rating, sample))

    def _pivot(dim_map: dict[str, list]) -> dict:
        """dim_value → model → stats"""
        result = {}
        for dim_val, dim_rows in sorted(dim_map.items()):
            result[dim_val] = {}
            for m in sorted(models):
                filtered = [r for r in dim_rows if r[1].model == m]
                result[dim_val][m] = _build_stats(filtered)
        return result

    total_ratings = len(rows)
    evaluated_groups = (
        db.query(func.count(EvalGroup.id))
        .filter(EvalGroup.eval_task_id == task_id, EvalGroup.eval_count > 0)
        .scalar() or 0
    )

    return {
        "task_id": task_id,
        "task_name": t.name,
        "metrics": t.get_metrics(),
        "total_ratings": total_ratings,
        "evaluated_groups": evaluated_groups,
        "total_groups": t.total_groups,
        # 总体
        "by_model": {
            m: _build_stats(rs) for m, rs in sorted(models.items())
        },
        # 按测试集
        "by_test_set": _pivot(test_sets),
        # 按性别
        "by_gender": _pivot(genders),
        # 按年龄
        "by_age": _pivot(ages),
        # 性别 × 年龄 综合
        "by_gender_age": _pivot(gender_ages),
    }


@router.post("/tasks/{task_id}/groups/{group_id}/release")
def release_checkout(task_id: str, group_id: str, body: ReleaseCheckoutRequest, db: Session = Depends(get_db)):
    """已废弃乐观锁，保留端点供旧客户端兼容，直接返回 ok。"""
    return {"ok": True}


@router.delete("/tasks/{task_id}")
def delete_eval_task(task_id: str, db: Session = Depends(get_db)):
    """删除评测任务及其所有数据（不删除音频文件）。"""
    t = db.get(EvalTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="评测任务不存在")

    db.query(EvalRating).filter(EvalRating.eval_task_id == task_id).delete()
    db.query(EvalSample).filter(EvalSample.eval_task_id == task_id).delete()
    db.query(EvalGroup).filter(EvalGroup.eval_task_id == task_id).delete()
    db.delete(t)
    db.commit()
    return {"ok": True}
