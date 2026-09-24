"""SubprocessExecutor：在 conda/venv 环境中启动 runner 脚本，解析 stdout JSON lines。"""
import json
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.executors.base import BaseExecutor, ExecutorFactory, ExecutorResult

MODEL_EXECUTION_TIMEOUT = 3600  # 含模型加载，设宽裕
from backend.config import OUTPUTS_DIR as OUTPUTS_BASE


def resolve_prompt_path(raw_source_path: str, global_path: str | None, prompt_map: dict) -> str:
    # 全局 Prompt 无条件覆盖所有条目
    if global_path:
        return global_path
    # 空路径：查 prompt_map[""] 兜底
    if not raw_source_path:
        if "" in prompt_map:
            return prompt_map[""]
        raise FileNotFoundError("source_path 为空且未配置映射，请上传对应 Prompt 或全局 Prompt")
    # 非空路径：prompt_map 显式映射 > 服务器原始文件
    basename = os.path.basename(raw_source_path)
    if basename in prompt_map:
        return prompt_map[basename]
    if os.path.isfile(raw_source_path):
        return raw_source_path
    raise FileNotFoundError(
        f"prompt 文件不存在: {raw_source_path}，请上传或配置映射"
    )


def build_resolved_jsonl(jsonl_path: str, global_prompt: str | None, prompt_map: dict) -> str:
    """将 JSONL 的 source_path 解析为实际路径，写入临时文件，返回路径。"""
    resolved_lines = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            raw = item.get("source_path", "")
            item["source_path"] = resolve_prompt_path(raw, global_prompt, prompt_map)
            resolved_lines.append(json.dumps(item, ensure_ascii=False))

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    tmp.write("\n".join(resolved_lines) + "\n")
    tmp.close()
    return tmp.name


def build_cmd(model, runner_script: str, resolved_jsonl: str, output_dir: str, task_id: str, params: dict) -> list[str]:
    args = [
        runner_script,
        "--jsonl", resolved_jsonl,
        "--output-dir", output_dir,
        "--task-id", task_id,
        "--params", json.dumps(params),
    ]
    if model.model_dir:
        args += ["--model-dir", model.model_dir]

    if model.conda_env:
        return ["conda", "run", "--no-capture-output", "-n", model.conda_env, "python"] + args
    elif model.venv_python:
        return [model.venv_python] + args
    else:
        raise ValueError(f"模型 {model.id} 既无 conda_env 也无 venv_python")


class SubprocessExecutor(BaseExecutor):
    def execute(self, task, model, db: Session) -> ExecutorResult:
        input_data = json.loads(task.input or "{}")
        global_prompt = input_data.get("global_prompt_path")
        prompt_map = input_data.get("prompt_map", {})
        params = input_data.get("params", {})

        output_dir = str(OUTPUTS_BASE / task.id)
        os.makedirs(output_dir, exist_ok=True)

        # 支持直接传 items 或从 jsonl_path 读取
        if "items" in input_data:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
            )
            for item in input_data["items"]:
                src = item.get("source_path", "")
                if src and os.path.isfile(src):
                    pass
                elif global_prompt:
                    item["source_path"] = global_prompt
                tmp.write(json.dumps(item, ensure_ascii=False) + "\n")
            tmp.close()
            resolved_jsonl = tmp.name
        else:
            jsonl_path = input_data["jsonl_path"]
            try:
                resolved_jsonl = build_resolved_jsonl(jsonl_path, global_prompt, prompt_map)
            except FileNotFoundError as e:
                task.append_log(str(e), "error")
                db.commit()
                raise

        cmd = build_cmd(model, model.runner_script, resolved_jsonl, output_dir, task.id, params)

        task.append_log(f"启动子进程: {' '.join(cmd[:6])}...")
        db.commit()

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        t0 = time.time()
        result = ExecutorResult()

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )

            # stderr 独立线程读取
            stderr_lines: list[str] = []

            def _read_stderr():
                for line in process.stderr:
                    stderr_lines.append(line.rstrip())

            stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
            stderr_thread.start()

            # 主线程读取 stdout JSON lines
            for raw_line in process.stdout:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    task.append_log(raw_line, "debug")
                    db.commit()
                    continue

                etype = event.get("event")
                if etype == "ready":
                    total = event.get("total", 0)
                    elapsed = event.get("elapsed", 0)
                    result.total = total
                    task.append_log(f"模型就绪，共 {total} 条，加载耗时 {elapsed:.1f}s")
                    db.commit()

                elif etype == "item":
                    key = event.get("key", "")
                    status = event.get("status", "fail")
                    item_elapsed = event.get("elapsed", 0)
                    error = event.get("error", "")

                    if status == "ok":
                        result.ok += 1
                        audio_url = f"/storage/outputs/{task.id}/{key}.wav"
                        result.items.append({"key": key, "status": "success", "audio_url": audio_url})
                        task.append_log(f"[{result.ok + result.fail}/{result.total}] {key}: ok ({item_elapsed:.1f}s)")
                    elif status == "skip":
                        task.append_log(f"[skip] {key}")
                    else:
                        result.fail += 1
                        result.items.append({"key": key, "status": "failed", "error": error})
                        task.append_log(f"[{result.ok + result.fail}/{result.total}] {key}: FAIL - {error}", "error")
                    db.commit()

                elif etype == "done":
                    ok = event.get("ok", 0)
                    fail = event.get("fail", 0)
                    task.append_log(f"Runner 完成：{ok} 成功 / {fail} 失败，耗时 {time.time() - t0:.1f}s")
                    db.commit()

            process.wait(timeout=MODEL_EXECUTION_TIMEOUT)
            stderr_thread.join(timeout=5)

            # 过滤掉 triton autotuning 的 DEBUG 噪音行
            def _is_noise(line: str) -> bool:
                return " DEBUG " in line and (".triton" in line or ".lock" in line)

            if process.returncode != 0:
                meaningful = [l for l in stderr_lines if not _is_noise(l)]
                stderr_tail = "\n".join(meaningful[-20:])
                task.append_log(f"Runner 退出码 {process.returncode}:\n{stderr_tail}", "error")
                db.commit()
                if result.ok == 0 and result.fail == 0:
                    raise RuntimeError(f"Runner 失败 (rc={process.returncode}): {meaningful[-1] if meaningful else ''}")

            elif stderr_lines:
                # returncode==0 时 stderr 仍记录为 debug，跳过纯噪音行
                meaningful = [l for l in stderr_lines[-20:] if not _is_noise(l)]
                for line in meaningful[-5:]:
                    task.append_log(line, "debug")
                db.commit()

        except subprocess.TimeoutExpired:
            process.kill()
            raise RuntimeError(f"Runner 超时 (>{MODEL_EXECUTION_TIMEOUT}s)")
        finally:
            # 清理临时 JSONL
            try:
                os.unlink(resolved_jsonl)
            except OSError:
                pass

        if result.total == 0:
            result.total = result.ok + result.fail

        return result


ExecutorFactory.register("direct_python", SubprocessExecutor)
