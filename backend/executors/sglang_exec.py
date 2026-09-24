"""SglangExecutor：通过 sglang-omni HTTP 服务执行 TTS 推理。"""
import json
import os
import time
from pathlib import Path

from sqlalchemy.orm import Session

from backend.executors.base import BaseExecutor, ExecutorFactory, ExecutorResult
from backend.executors.sglang_manager import ServiceManager, SERVICE_PORT

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

from backend.config import OUTPUTS_DIR as OUTPUTS_BASE
API_URL = f"http://localhost:{SERVICE_PORT}/v1/audio/speech"
ITEM_TIMEOUT = 120  # 每条合成超时


def _load_jsonl(path: str) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _resolve_prompt(item: dict, global_prompt: str | None, prompt_map: dict) -> str:
    if global_prompt:
        return global_prompt
    raw = item.get("source_path", "")
    basename = os.path.basename(raw)
    if basename in prompt_map:
        return prompt_map[basename]
    return raw


def _build_payload(item: dict, prompt_path: str, request_format: str, params: dict) -> dict:
    text = item.get("text", "")
    source_text = item.get("source_text", "")

    if request_format == "moss":
        payload = {
            "input": text,
            "ref_audio": prompt_path,
            "ref_text": source_text,
            "max_new_tokens": params.get("max_new_tokens", 1024),
        }
    else:  # higgs（默认）
        payload = {
            "input": text,
            "references": [{"audio_path": prompt_path, "text": source_text}],
            "temperature": params.get("temperature", 0.8),
            "top_k": params.get("top_k", 50),
            "max_new_tokens": params.get("max_new_tokens", 1024),
        }
    return payload


class SglangExecutor(BaseExecutor):
    def execute(self, task, model, db: Session) -> ExecutorResult:
        if not _REQUESTS_OK:
            raise ImportError("requests 未安装")

        input_data = json.loads(task.input or "{}")
        global_prompt = input_data.get("global_prompt_path")
        prompt_map = input_data.get("prompt_map", {})
        params = input_data.get("params", {})

        output_dir = str(OUTPUTS_BASE / task.id)
        os.makedirs(output_dir, exist_ok=True)

        extra = json.loads(model.extra_config or "{}")
        request_format = extra.get("request_format", "higgs")

        # 支持直接传 items 或从 jsonl_path 读取
        if "items" in input_data:
            items = input_data["items"]
        else:
            jsonl_path = input_data["jsonl_path"]
            items = _load_jsonl(jsonl_path)

        result = ExecutorResult(total=len(items))

        # 启动/切换 sglang-omni 服务
        task.append_log(f"启动 sglang-omni 服务（模型: {model.id}）...")
        db.commit()

        mgr = ServiceManager()
        mgr.ensure_running(model)
        task.append_log("服务就绪")
        db.commit()

        ok = fail = 0
        for item in items:
            key = item.get("key", "")
            out_path = os.path.join(output_dir, f"{key}.wav")

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                task.append_log(f"[skip] {key}")
                db.commit()
                continue

            prompt_path = _resolve_prompt(item, global_prompt, prompt_map)
            payload = _build_payload(item, prompt_path, request_format, params)

            t0 = time.time()
            try:
                resp = requests.post(API_URL, json=payload, timeout=ITEM_TIMEOUT)
                resp.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                ok += 1
                elapsed = round(time.time() - t0, 2)
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[{ok + fail}/{len(items)}] {key}: ok ({elapsed}s)")
            except Exception as e:
                fail += 1
                result.items.append({"key": key, "status": "failed", "error": str(e)})
                task.append_log(f"[{ok + fail}/{len(items)}] {key}: FAIL - {e}", "error")
            db.commit()

        result.ok = ok
        result.fail = fail
        task.append_log(f"完成：{ok} 成功 / {fail} 失败")
        db.commit()
        return result


ExecutorFactory.register("sglang", SglangExecutor)
