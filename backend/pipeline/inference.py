import json
from pathlib import Path
from datetime import datetime
from sqlalchemy.orm import Session

from backend.models.task import Task
from backend.adapters.base import TTSItem
from backend.adapters.minimax import MiniMaxAdapter
from backend.config import MINIMAX_API_URL, MINIMAX_API_KEY, OUTPUTS_DIR


def _load_jsonl(path: str) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _save_audio(audio_bytes: bytes, task_id: str, key: str) -> str:
    out_dir = OUTPUTS_DIR / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{key}.wav"
    with open(out_path, "wb") as f:
        f.write(audio_bytes)
    return f"/storage/outputs/{task_id}/{key}.wav"


def run_inference_pipeline(task: Task, db: Session) -> dict:
    input_data = json.loads(task.input)
    jsonl_path = input_data["jsonl_path"]
    global_prompt_path = input_data.get("global_prompt_path")
    params = input_data.get("params", {})

    items = _load_jsonl(jsonl_path)
    adapter = MiniMaxAdapter(MINIMAX_API_URL, MINIMAX_API_KEY)
    results = []

    task.append_log(f"开始推理，共 {len(items)} 条")
    db.commit()

    for i, raw in enumerate(items):
        key = raw.get("key", f"item_{i:04d}")
        text = raw.get("text", "")
        prompt_path = global_prompt_path or raw.get("source_path", "")
        prompt_text = raw.get("source_text", "")
        emotion = raw.get("emotion", "")

        tts_item = TTSItem(
            key=key,
            text=text,
            prompt_audio_path=prompt_path,
            prompt_text=prompt_text,
            emotion=emotion,
        )

        try:
            result = adapter.synthesize(tts_item, params)
            if result.error:
                results.append({"key": key, "status": "failed", "error": result.error, "text": text})
                task.append_log(f"[{i+1}/{len(items)}] {key}: FAILED - {result.error}", "error")
            else:
                audio_url = _save_audio(result.audio_bytes, task.id, key)
                results.append({"key": key, "status": "success", "audio_url": audio_url, "text": text})
                task.append_log(f"[{i+1}/{len(items)}] {key}: ok")
        except Exception as e:
            results.append({"key": key, "status": "failed", "error": str(e), "text": text})
            task.append_log(f"[{i+1}/{len(items)}] {key}: EXCEPTION - {e}", "error")

        db.commit()

    success = sum(1 for r in results if r["status"] == "success")
    failed = len(results) - success
    task.append_log(f"完成：{success} 成功 / {failed} 失败")
    db.commit()

    return {
        "items": results,
        "total": len(results),
        "success": success,
        "failed": failed,
    }
