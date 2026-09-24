"""APIExecutor：调用 HTTP API 的 TTS 模型（ElevenLabs、MIMO、Minimax、Doubao）。"""
import base64
import hashlib
import json
import os
import threading
import time
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from datetime import datetime

import requests
from sqlalchemy.orm import Session

from backend.executors.base import BaseExecutor, ExecutorFactory, ExecutorResult

from backend.config import OUTPUTS_DIR as OUTPUTS_BASE


def _update_parent_progress(db: Session, input_data: dict, ok: int, fail: int) -> None:
    """逐条更新父任务的 synthesis_progress.done，供 SSE 实时推送使用。"""
    parent_task_id = input_data.get("parent_task_id")
    parent_offset  = input_data.get("parent_synth_offset", 0)
    if not parent_task_id:
        return
    from backend.models.task import Task as TaskModel
    try:
        ptask = db.get(TaskModel, parent_task_id)
        if ptask and ptask.input:
            pinp = json.loads(ptask.input)
            prog = pinp.get("synthesis_progress")
            if prog:
                prog["done"] = parent_offset + ok + fail
                ptask.input = json.dumps(pinp, ensure_ascii=False)
                db.commit()
    except Exception:
        pass  # 进度更新失败不影响主流程


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
    if not raw:
        if "" in prompt_map:
            return prompt_map[""]
        raise FileNotFoundError("source_path 为空且未配置映射，请上传对应 Prompt 或全局 Prompt")
    basename = os.path.basename(raw)
    if basename in prompt_map:
        return prompt_map[basename]
    if os.path.isfile(raw):
        return raw
    raise FileNotFoundError(f"prompt 文件不存在: {raw}，请上传或配置映射")


# ─────────────────────────────────────────────
# ElevenLabs Adapter
# ─────────────────────────────────────────────

class ElevenLabsAdapter:
    # per-prompt_hash 锁：防止并发任务用同一 prompt 重复创建音色（浪费 API 配额 + 产生孤儿记录）
    _voice_locks: dict[str, threading.Lock] = {}
    _voice_locks_guard = threading.Lock()

    def __init__(self, api_key: str):
        from elevenlabs.client import ElevenLabs as _EL
        self.client = _EL(api_key=api_key)

    @classmethod
    def _get_voice_lock(cls, prompt_hash: str) -> threading.Lock:
        with cls._voice_locks_guard:
            if prompt_hash not in cls._voice_locks:
                cls._voice_locks[prompt_hash] = threading.Lock()
            return cls._voice_locks[prompt_hash]

    @staticmethod
    def file_hash(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]

    def get_or_create_voice(self, prompt_path: str, task_id: str, db: Session) -> tuple[str, bool]:
        """返回 (voice_id, was_created)。相同音频内容复用已有音色。
        使用 per-prompt_hash 锁防止并发创建同一音色。"""
        from backend.models.task import ElevenLabsVoice

        prompt_hash = self.file_hash(prompt_path)

        # 快速路径：不加锁先查一次，大概率已存在
        existing = db.query(ElevenLabsVoice).filter(
            ElevenLabsVoice.prompt_hash == prompt_hash,
            ElevenLabsVoice.deleted_at.is_(None),
        ).first()
        if existing:
            return existing.voice_id, False

        # 需要创建：加 per-hash 锁，防止并发重复创建
        with self._get_voice_lock(prompt_hash):
            # double-check：拿到锁后再查一次
            existing = db.query(ElevenLabsVoice).filter(
                ElevenLabsVoice.prompt_hash == prompt_hash,
                ElevenLabsVoice.deleted_at.is_(None),
            ).first()
            if existing:
                return existing.voice_id, False

            voice_name = f"awp_{prompt_hash}"
            voice = self.client.voices.ivc.create(
                name=voice_name,
                files=[BytesIO(open(prompt_path, "rb").read())],
                description=f"AI Workflow Platform {prompt_hash}",
            )
            record = ElevenLabsVoice(
                voice_id=voice.voice_id,
                voice_name=voice_name,
                prompt_hash=prompt_hash,
                task_id=task_id,
            )
            db.add(record)
            db.commit()
            return voice.voice_id, True

    def delete_voice(self, voice_id: str, db: Session) -> None:
        from backend.models.task import ElevenLabsVoice
        try:
            self.client.voices.delete(voice_id)
        except Exception:
            pass
        record = db.query(ElevenLabsVoice).filter(
            ElevenLabsVoice.voice_id == voice_id
        ).first()
        if record:
            record.deleted_at = datetime.utcnow()
            db.commit()

    def synthesize(self, text: str, voice_id: str, params: dict) -> bytes:
        audio_iter = self.client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id="eleven_v3",
            output_format="wav_44100",
            voice_settings={
                "stability": params.get("stability", 0.5),
                "similarity_boost": params.get("similarity_boost", 0.75),
            },
        )
        return b"".join(audio_iter)


def _run_elevenlabs(task, model, items: list[dict], output_dir: str,
                    global_prompt: str | None, prompt_map: dict,
                    params: dict, extra: dict, db: Session) -> ExecutorResult:
    input_data = json.loads(task.input or "{}")
    api_key = os.environ.get(extra["api_key_env"], "")
    delete_after_task = bool(params.get("delete_after_task", False))
    adapter = ElevenLabsAdapter(api_key=api_key)

    result = ExecutorResult(total=len(items))
    task_voice_cache: dict[str, str] = {}   # prompt_hash → voice_id（任务内去重）
    created_voice_ids: list[str] = []        # 本次任务新建的 voice_id（用于事后清理）

    # 按 resolved prompt path 分组
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        prompt_path = _resolve_prompt(item, global_prompt, prompt_map)
        groups[prompt_path].append(item)

    ok = fail = 0
    for prompt_path, group_items in groups.items():
        # 获取或创建音色
        try:
            prompt_hash = ElevenLabsAdapter.file_hash(prompt_path)
            if prompt_hash in task_voice_cache:
                voice_id = task_voice_cache[prompt_hash]
            else:
                voice_id, was_created = adapter.get_or_create_voice(prompt_path, task.id, db)
                task_voice_cache[prompt_hash] = voice_id
                if was_created:
                    created_voice_ids.append(voice_id)
                    task.append_log(f"[voice] 新建音色 awp_{prompt_hash}")
                else:
                    task.append_log(f"[voice] 复用音色 awp_{prompt_hash}")
                db.commit()
        except Exception as e:
            for item in group_items:
                fail += 1
                result.items.append({"key": item.get("key", ""), "status": "failed",
                                     "error": f"voice clone: {e}"})
            task.append_log(f"[voice FAIL] {e}", "error")
            db.commit()
            continue

        for item in group_items:
            key = item.get("key", "")
            out_path = os.path.join(output_dir, f"{key}.wav")

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[skip] {key}")
                db.commit()
                continue

            t0 = time.time()
            try:
                audio_bytes = adapter.synthesize(item.get("text", ""), voice_id, params)
                with open(out_path, "wb") as f:
                    f.write(audio_bytes)
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[{ok + fail}/{len(items)}] {key}: ok ({time.time() - t0:.1f}s)")
            except Exception as e:
                # voice_not_found → 重建后重试一次
                if "voice_not_found" in str(e):
                    try:
                        from backend.models.task import ElevenLabsVoice
                        rec = db.query(ElevenLabsVoice).filter(
                            ElevenLabsVoice.voice_id == voice_id
                        ).first()
                        if rec:
                            rec.deleted_at = datetime.utcnow()
                            db.commit()
                        task_voice_cache.pop(prompt_hash, None)
                        new_vid, _ = adapter.get_or_create_voice(prompt_path, task.id, db)
                        task_voice_cache[prompt_hash] = new_vid
                        created_voice_ids.append(new_vid)
                        audio_bytes = adapter.synthesize(item.get("text", ""), new_vid, params)
                        with open(out_path, "wb") as f:
                            f.write(audio_bytes)
                        ok += 1
                        result.items.append({"key": key, "status": "success",
                                             "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                        task.append_log(f"[{ok + fail}/{len(items)}] {key}: ok (retry)")
                        db.commit()
                        time.sleep(0.3)
                        continue
                    except Exception as e2:
                        e = e2
                fail += 1
                result.items.append({"key": key, "status": "failed", "error": str(e)})
                task.append_log(f"[{ok + fail}/{len(items)}] {key}: FAIL - {e}", "error")
            db.commit()
            _update_parent_progress(db, input_data, ok, fail)
            time.sleep(0.3)

    # 任务结束后清理本次新建的音色
    if delete_after_task and created_voice_ids:
        task.append_log(f"清理本次新建的 {len(created_voice_ids)} 个音色...")
        db.commit()
        for vid in created_voice_ids:
            try:
                adapter.delete_voice(vid, db)
            except Exception as e:
                task.append_log(f"[voice delete FAIL] {vid}: {e}", "error")
        task.append_log("音色清理完成")
        db.commit()

    result.ok = ok
    result.fail = fail
    return result


# ─────────────────────────────────────────────
# MIMO Adapter
# ─────────────────────────────────────────────

def _run_mimo(task, model, items: list[dict], output_dir: str,
              global_prompt: str | None, prompt_map: dict,
              params: dict, extra: dict, db: Session) -> ExecutorResult:
    input_data = json.loads(task.input or "{}")
    from openai import OpenAI

    api_key = os.environ.get(extra["api_key_env"], "")
    client = OpenAI(api_key=api_key, base_url=extra["api_base_url"])
    model_name = extra.get("model_name", "mimo-v2.5-tts-voiceclone")

    result = ExecutorResult(total=len(items))
    ok = fail = 0

    for item in items:
        key = item.get("key", "")
        out_path = os.path.join(output_dir, f"{key}.wav")

        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            task.append_log(f"[skip] {key}")
            db.commit()
            continue

        prompt_path = _resolve_prompt(item, global_prompt, prompt_map)
        with open(prompt_path, "rb") as f:
            voice_b64 = base64.b64encode(f.read()).decode()

        t0 = time.time()
        try:
            instruction = item.get("instruction") or params.get("instruction", "")
            completion = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "user", "content": instruction},
                    {"role": "assistant", "content": item.get("text", "")},
                ],
                audio={
                    "format": "wav",
                    "voice": f"data:audio/wav;base64,{voice_b64}",
                },
            )
            audio_bytes = base64.b64decode(completion.choices[0].message.audio.data)
            with open(out_path, "wb") as f:
                f.write(audio_bytes)
            ok += 1
            result.items.append({"key": key, "status": "success",
                                  "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
            task.append_log(f"[{ok + fail}/{len(items)}] {key}: ok ({time.time() - t0:.1f}s)")
        except Exception as e:
            fail += 1
            result.items.append({"key": key, "status": "failed", "error": str(e)})
            task.append_log(f"[{ok + fail}/{len(items)}] {key}: FAIL - {e}", "error")
        db.commit()
        _update_parent_progress(db, input_data, ok, fail)
        time.sleep(0.5)

    result.ok = ok
    result.fail = fail
    return result


# ─────────────────────────────────────────────
# Minimax Adapter
# ─────────────────────────────────────────────

class MinimaxAdapter:
    def __init__(self, api_url: str, upload_url: str, auth: str):
        self.api_url    = api_url
        self.upload_url = upload_url
        self.headers    = {"Authorization": f"Bearer {auth}", "Content-Type": "application/json"}

    @staticmethod
    def file_hash(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]

    def get_or_create_voice(self, prompt_path: str, task_id: str, db: Session) -> tuple[str, bool]:
        from backend.models.task import MinimaxVoice
        import soundfile as sf

        prompt_hash = self.file_hash(prompt_path)
        existing = db.query(MinimaxVoice).filter(
            MinimaxVoice.prompt_hash == prompt_hash,
            MinimaxVoice.deleted_at.is_(None),
        ).first()
        if existing:
            return existing.provider_voice_id, False

        # 检查音频时长，minimax 克隆要求 ≥ 10 秒
        try:
            duration = sf.info(prompt_path).duration
            if duration < 10.0:
                raise ValueError(
                    f"Minimax 克隆要求参考音频 ≥ 10 秒，当前音频仅 {duration:.1f} 秒，请更换更长的 Prompt 音频"
                )
        except ValueError:
            raise
        except Exception:
            pass  # 无法读取时长则跳过检查，让服务端决定

        # Step 1: 上传音频
        with open(prompt_path, "rb") as f:
            audio_bytes = f.read()
        upload_headers = {"X-Request-Id": hashlib.md5(audio_bytes[:128]).hexdigest()}
        resp = requests.post(
            self.upload_url,
            files={"file": ("audio.wav", audio_bytes, "audio/wav")},
            data={"purpose": "voice_clone"},
            headers=upload_headers,
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()
        if str(result.get("code")) != "10000":
            raise RuntimeError(f"minimax upload failed: {result}")
        file_id = int(result["data"]["file_id"])

        # Step 2: 克隆
        provider_voice_id = f"L50-Minimax-{prompt_hash}"
        clone_payload = {
            "model":                     "minimax-voice-clone-speech-2.8-turbo",
            "file_id":                   file_id,
            "voice_id":                  provider_voice_id,
            "need_noise_reduction":      False,
            "need_volume_normalization":  False,
            "aigc_watermark":            False,
        }
        resp = requests.post(self.api_url, json=clone_payload, headers=self.headers, timeout=120)
        resp.raise_for_status()
        result = resp.json()
        if result.get("base_resp", {}).get("status_code") != 0:
            raise RuntimeError(f"minimax clone failed: {result}")

        record = MinimaxVoice(
            provider_voice_id=provider_voice_id,
            prompt_hash=prompt_hash,
            task_id=task_id,
        )
        db.add(record)
        db.commit()
        return provider_voice_id, True

    def delete_voice(self, provider_voice_id: str, db: Session) -> None:
        from backend.models.task import MinimaxVoice
        for voice_type in ("voice_cloning", "voice_generation"):
            payload = {
                "model":      "minimax-voice-delete",
                "voice_type": voice_type,
                "voice_id":   provider_voice_id,
            }
            resp = requests.post(self.api_url, json=payload, headers=self.headers, timeout=60)
            if resp.ok:
                result = resp.json()
                if result.get("base_resp", {}).get("status_code") == 0:
                    break
        record = db.query(MinimaxVoice).filter(
            MinimaxVoice.provider_voice_id == provider_voice_id
        ).first()
        if record:
            record.deleted_at = datetime.utcnow()
            db.commit()

    def synthesize(self, text: str, provider_voice_id: str, params: dict) -> bytes:
        payload = {
            "model": "minimax-tts-speech-2.8-hd",
            "text":  text,
            "stream": False,
            "voice_setting": {
                "voice_id": provider_voice_id,
                "speed": params.get("speed", 1),
                "vol":   params.get("vol", 1),
                "pitch": params.get("pitch", 0),
            },
            "audio_setting": {
                "sample_rate": 24000,
                "format":      "wav",
                "channel":     1,
            },
        }
        resp = requests.post(self.api_url, json=payload, headers=self.headers, timeout=300)
        resp.raise_for_status()
        result = resp.json()
        if result.get("base_resp", {}).get("status_code") != 0:
            raise RuntimeError(f"minimax tts failed: {result}")
        return bytes.fromhex(result["data"]["audio"])


def _run_minimax(task, model, items: list[dict], output_dir: str,
                 global_prompt: str | None, prompt_map: dict,
                 params: dict, extra: dict, db: Session) -> ExecutorResult:
    input_data = json.loads(task.input or "{}")
    auth       = os.environ.get(extra.get("auth_env", "TTS_AUTH"), "")
    api_url    = extra["api_url"]
    upload_url = extra.get("upload_url", os.environ.get("MINIMAX_UPLOAD_URL", ""))
    delete_after_task = bool(params.get("delete_after_task", False))
    adapter = MinimaxAdapter(api_url=api_url, upload_url=upload_url, auth=auth)

    result = ExecutorResult(total=len(items))
    task_voice_cache: dict[str, str] = {}
    created_voice_ids: list[str] = []

    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        prompt_path = _resolve_prompt(item, global_prompt, prompt_map)
        groups[prompt_path].append(item)

    ok = fail = 0
    for prompt_path, group_items in groups.items():
        try:
            prompt_hash = MinimaxAdapter.file_hash(prompt_path)
            if prompt_hash in task_voice_cache:
                voice_id = task_voice_cache[prompt_hash]
            else:
                voice_id, was_created = adapter.get_or_create_voice(prompt_path, task.id, db)
                task_voice_cache[prompt_hash] = voice_id
                if was_created:
                    created_voice_ids.append(voice_id)
                    task.append_log(f"[minimax voice] 新建: {voice_id}")
                else:
                    task.append_log(f"[minimax voice] 复用: {voice_id}")
                db.commit()
        except Exception as e:
            for item in group_items:
                fail += 1
                result.items.append({"key": item.get("key", ""), "status": "failed",
                                     "error": f"voice clone: {e}"})
            task.append_log(f"[minimax voice FAIL] {e}", "error")
            db.commit()
            continue

        for item in group_items:
            key = item.get("key", "")
            out_path = os.path.join(output_dir, f"{key}.wav")
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[skip] {key}")
                db.commit()
                continue
            t0 = time.time()
            try:
                audio_bytes = adapter.synthesize(item.get("text", ""), voice_id, params)
                with open(out_path, "wb") as f:
                    f.write(audio_bytes)
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[{ok+fail}/{len(items)}] {key}: ok ({time.time()-t0:.1f}s)")
            except Exception as e:
                fail += 1
                result.items.append({"key": key, "status": "failed", "error": str(e)})
                task.append_log(f"[{ok+fail}/{len(items)}] {key}: FAIL - {e}", "error")
            db.commit()
            _update_parent_progress(db, input_data, ok, fail)
            time.sleep(0.3)

    if delete_after_task and created_voice_ids:
        task.append_log(f"清理本次新建的 {len(created_voice_ids)} 个 minimax 音色…")
        db.commit()
        for vid in created_voice_ids:
            try:
                adapter.delete_voice(vid, db)
            except Exception as e:
                task.append_log(f"[minimax delete FAIL] {vid}: {e}", "error")
        task.append_log("minimax 音色清理完成")
        db.commit()

    result.ok = ok
    result.fail = fail
    return result


# ─────────────────────────────────────────────
# Doubao Adapter
# ─────────────────────────────────────────────

class DoubaoAdapter:
    def __init__(self, api_url: str, apply_url: str, auth: str,
                 apply_app_id: str, apply_project_id: str, user: str):
        self.api_url            = api_url
        self.apply_url          = apply_url
        self.apply_app_id       = apply_app_id
        self.apply_project_id   = apply_project_id
        self.user               = user
        self.clone_headers = {
            "Authorization": f"Bearer {auth}",
            "Content-Type":  "application/json",
        }
        self.apply_headers = {
            "x-app-id":     apply_app_id,
            "x-project-id": apply_project_id,
            "Content-Type": "application/json",
        }

    @staticmethod
    def file_hash(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]

    def get_or_create_voice(self, prompt_path: str, prompt_text: str,
                            task_id: str, db: Session) -> tuple[str, bool]:
        from backend.models.task import DoubaoVoice

        prompt_hash = self.file_hash(prompt_path)
        existing = db.query(DoubaoVoice).filter(
            DoubaoVoice.prompt_hash == prompt_hash,
        ).first()
        if existing:
            return existing.speaker_id, False

        # Step 1: 申请 speaker_id
        apply_payload = {
            "app_id":     self.apply_app_id,
            "project_id": self.apply_project_id,
            "user":       self.user,
        }
        resp = requests.post(self.apply_url, json=apply_payload,
                             headers=self.apply_headers, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != "10000":
            raise RuntimeError(f"doubao apply speaker_id failed: {result}")
        speaker_id = result["data"]["speaker_id"]

        # Step 2: 克隆
        with open(prompt_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()
        clone_payload = {
            "model":      "seed-voice-clone",
            "speaker_id": speaker_id,
            "audio": {"data": audio_b64, "text": prompt_text},
            "language": 0,
        }
        resp = requests.post(self.api_url, json=clone_payload,
                             headers=self.clone_headers, timeout=120)
        resp.raise_for_status()
        result = resp.json()
        if result.get("httpCode", 200) != 200:
            raise RuntimeError(f"doubao clone failed: {result}")

        record = DoubaoVoice(
            speaker_id=speaker_id,
            prompt_hash=prompt_hash,
            task_id=task_id,
        )
        db.add(record)
        db.commit()
        return speaker_id, True

    def synthesize(self, text: str, speaker_id: str) -> bytes:
        payload = {
            "user": {"uid": "12345"},
            "req_params": {
                "text":   text,
                "speaker": speaker_id,
                "audio_params": {"format": "wav", "sample_rate": 24000},
            },
            "model": "seed-icl-2.0",
        }
        resp = requests.post(self.api_url, json=payload,
                             headers=self.clone_headers, timeout=300)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 20000000:
            raise RuntimeError(f"doubao tts failed: {result}")
        return base64.b64decode(result["data"])


def _run_doubao(task, model, items: list[dict], output_dir: str,
                global_prompt: str | None, prompt_map: dict,
                params: dict, extra: dict, db: Session) -> ExecutorResult:
    input_data         = json.loads(task.input or "{}")
    auth               = os.environ.get(extra.get("auth_env", "TTS_AUTH"), "")
    api_url            = extra["api_url"]
    apply_url          = extra["apply_url"]
    apply_app_id       = extra["apply_app_id"]
    apply_project_id   = extra["apply_project_id"]
    user               = extra.get("user", os.getenv("DOUBAO_USER", ""))

    adapter = DoubaoAdapter(
        api_url=api_url, apply_url=apply_url, auth=auth,
        apply_app_id=apply_app_id, apply_project_id=apply_project_id, user=user,
    )

    result = ExecutorResult(total=len(items))
    task_voice_cache: dict[str, str] = {}

    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        prompt_path = _resolve_prompt(item, global_prompt, prompt_map)
        groups[prompt_path].append(item)

    ok = fail = 0
    for prompt_path, group_items in groups.items():
        try:
            prompt_hash = DoubaoAdapter.file_hash(prompt_path)
            if prompt_hash in task_voice_cache:
                speaker_id = task_voice_cache[prompt_hash]
            else:
                # 用第一条 item 的 source_text 作为克隆参考文本（可为空）
                prompt_text = group_items[0].get("source_text", "") if group_items else ""
                speaker_id, was_created = adapter.get_or_create_voice(
                    prompt_path, prompt_text, task.id, db
                )
                task_voice_cache[prompt_hash] = speaker_id
                if was_created:
                    task.append_log(f"[doubao voice] 新建: {speaker_id}")
                else:
                    task.append_log(f"[doubao voice] 复用: {speaker_id}")
                db.commit()
        except Exception as e:
            for item in group_items:
                fail += 1
                result.items.append({"key": item.get("key", ""), "status": "failed",
                                     "error": f"voice clone: {e}"})
            task.append_log(f"[doubao voice FAIL] {e}", "error")
            db.commit()
            continue

        for item in group_items:
            key = item.get("key", "")
            out_path = os.path.join(output_dir, f"{key}.wav")
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[skip] {key}")
                db.commit()
                continue
            t0 = time.time()
            try:
                audio_bytes = adapter.synthesize(item.get("text", ""), speaker_id)
                with open(out_path, "wb") as f:
                    f.write(audio_bytes)
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[{ok+fail}/{len(items)}] {key}: ok ({time.time()-t0:.1f}s)")
            except Exception as e:
                fail += 1
                result.items.append({"key": key, "status": "failed", "error": str(e)})
                task.append_log(f"[{ok+fail}/{len(items)}] {key}: FAIL - {e}", "error")
            db.commit()
            _update_parent_progress(db, input_data, ok, fail)
            time.sleep(0.3)

    result.ok = ok
    result.fail = fail
    return result


# ─────────────────────────────────────────────
# VoxCPM2 Triton gRPC Adapter
# ─────────────────────────────────────────────

def _make_triton_client(server: str, extra: dict):
    import tritonclient.grpc as grpcclient
    keepalive = grpcclient.KeepAliveOptions(
        keepalive_time_ms=int(extra.get("keepalive_time_ms", 600000)),
        keepalive_timeout_ms=int(extra.get("keepalive_timeout_ms", 60000)),
        keepalive_permit_without_calls=True,
        http2_max_pings_without_data=2,
    )
    return grpcclient.InferenceServerClient(
        url=server, verbose=False, keepalive_options=keepalive
    )


def _voxcpm2_extract_features(client, audio_path: str, prompt_text: str, headers: dict) -> dict:
    """一阶段：提取音色特征，每个 prompt 音频只调一次。"""
    import tritonclient.grpc as grpcclient
    import numpy as np

    with open(audio_path, "rb") as f:
        wav_bytes = f.read()

    inputs = [
        grpcclient.InferInput("prompt_speech", [1, 1], "BYTES").set_data_from_numpy(
            np.asarray([wav_bytes]).reshape([1, 1])
        ),
        grpcclient.InferInput("prompt_text", [1, 1], "BYTES").set_data_from_numpy(
            np.asarray([prompt_text.encode("utf-8")]).reshape([1, 1])
        ),
    ]
    outputs = [grpcclient.InferRequestedOutput(k) for k in
               ("prompt_speech_feat", "ref_speech_feat", "prompt_text", "code", "message")]
    resp = client.infer("extract_feats", inputs, outputs=outputs, headers=headers)
    return {
        "prompt_speech_feat": resp.as_numpy("prompt_speech_feat"),
        "ref_speech_feat":    resp.as_numpy("ref_speech_feat"),
        # 使用服务端 ASR 返回的 prompt_text，不用原始传入的（服务端可能修正了格式）
        "prompt_text":        resp.as_numpy("prompt_text")[0].decode("utf-8"),
        "code":    int(resp.as_numpy("code")[0]),
        "message": resp.as_numpy("message")[0].decode("utf-8"),
    }


def _voxcpm2_tts_infer(client, text: str, feats: dict, headers: dict) -> dict:
    """二阶段：使用一阶段特征合成目标文本。注意不传 speaker 字段（会触发 500）。"""
    import tritonclient.grpc as grpcclient
    import numpy as np

    pf = feats["prompt_speech_feat"][np.newaxis].astype(np.float32)
    rf = feats["ref_speech_feat"][np.newaxis].astype(np.float32)

    inputs = [
        grpcclient.InferInput("text", [1, 1], "BYTES").set_data_from_numpy(
            np.asarray([text.encode("utf-8")]).reshape([1, 1])
        ),
        grpcclient.InferInput("prompt_text", [1, 1], "BYTES").set_data_from_numpy(
            np.asarray([feats["prompt_text"].encode("utf-8")]).reshape([1, 1])
        ),
        grpcclient.InferInput("prompt_speech_feat", list(pf.shape), "FP32").set_data_from_numpy(pf),
        grpcclient.InferInput("ref_speech_feat",    list(rf.shape), "FP32").set_data_from_numpy(rf),
    ]
    outputs = [grpcclient.InferRequestedOutput(k) for k in
               ("speech", "code", "message", "duration")]
    resp = client.infer("bls", inputs, outputs=outputs, headers=headers)
    return {
        "speech":   resp.as_numpy("speech")[0],
        "code":     int(resp.as_numpy("code")[0]),
        "message":  resp.as_numpy("message")[0].decode("utf-8"),
        "duration": float(resp.as_numpy("duration")[0]),
    }


def _run_voxcpm2_grpc(task, model, items: list[dict], output_dir: str,
                       global_prompt: str | None, prompt_map: dict,
                       params: dict, extra: dict, db: Session) -> ExecutorResult:
    input_data = json.loads(task.input or "{}")
    server  = extra["triton_server"]
    headers = {
        "service_id":     extra.get("service_id", "5000"),
        "sub_service_id": extra.get("sub_service_id", "5"),
    }

    client = _make_triton_client(server, extra)

    result = ExecutorResult(total=len(items))

    # 按 prompt 音频分组，同一音频的特征只提取一次
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        prompt_path = _resolve_prompt(item, global_prompt, prompt_map)
        groups[prompt_path].append(item)

    ok = fail = 0

    for prompt_path, group_items in groups.items():
        # Step 1: 特征提取（每个 prompt 只做一次）
        # 对 prompt 音频跑 ASR，确保 prompt_text 和音频内容一致
        # 不依赖 JSONL 里的 source_text（可能来自其他模型的 prompt，内容不匹配）
        try:
            from backend.tools import asr as asr_tool
            prompt_text = asr_tool.transcribe(prompt_path)
        except Exception:
            prompt_text = group_items[0].get("source_text", "")
        try:
            feats = _voxcpm2_extract_features(client, prompt_path, prompt_text, headers)
        except Exception as e:
            for item in group_items:
                fail += 1
                result.items.append({"key": item.get("key", ""), "status": "failed",
                                     "error": f"extract_feats: {e}"})
            task.append_log(f"[voxcpm2 extract FAIL] {e}", "error")
            db.commit()
            continue

        if feats["code"] not in (0, 200):
            for item in group_items:
                fail += 1
                result.items.append({"key": item.get("key", ""), "status": "failed",
                                     "error": f"extract_feats code={feats['code']}: {feats['message']}"})
            task.append_log(f"[voxcpm2 extract FAIL] code={feats['code']}: {feats['message']}", "error")
            db.commit()
            continue

        task.append_log(f"[voxcpm2] 特征提取完成，处理 {len(group_items)} 条")
        db.commit()

        # Step 2: 逐条 TTS
        for item in group_items:
            key = item.get("key", "")
            out_path = os.path.join(output_dir, f"{key}.wav")

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[skip] {key}")
                db.commit()
                continue

            t0 = time.time()
            try:
                tts = _voxcpm2_tts_infer(client, item.get("text", ""), feats, headers)
                if tts["code"] != 200:
                    raise RuntimeError(f"bls code={tts['code']}: {tts['message']}")
                # 退化输出检测：VoxCPM2 bls 可能返回 code=200 但实际是极短静音音频
                tts_duration = tts.get("duration", 0)
                if tts_duration < 0.5:
                    raise RuntimeError(
                        f"VoxCPM2 生成退化：duration={tts_duration:.2f}s（<0.5s），疑似静音/截断输出。"
                        f"常见原因：text 中的 control instruction 格式不被模型识别，或 prompt 特征退化。"
                        f"text={item.get('text', '')[:60]}"
                    )
                with open(out_path, "wb") as f:
                    f.write(tts["speech"])
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[{ok+fail}/{len(items)}] {key}: ok ({time.time()-t0:.1f}s)")
            except Exception as e:
                fail += 1
                result.items.append({"key": key, "status": "failed", "error": str(e)})
                task.append_log(f"[{ok+fail}/{len(items)}] {key}: FAIL - {e}", "error")
            db.commit()
            _update_parent_progress(db, input_data, ok, fail)
            time.sleep(0.1)

    result.ok = ok
    result.fail = fail
    return result


# ─────────────────────────────────────────────
# Qwen-Audio-3.0-TTS Adapter
# ─────────────────────────────────────────────

class QwenTtsAdapter:
    GATEWAY_URL = os.getenv("AIGC_GATEWAY_URL", "")
    MIN_DURATION_S = 10.0   # enrollment 最短音频要求

    def __init__(self, api_key: str, model_name: str):
        self.api_key    = api_key
        self.model_name = model_name   # qwen-audio-3.0-tts-flash 或 qwen-audio-3.0-tts-plus
        self.headers    = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-DashScope-OssResourceResolve": "enable",
        }

    @staticmethod
    def file_hash(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]

    def get_or_create_voice(self, prompt_path: str, task_id: str, db) -> tuple[str, bool]:
        from backend.models.task import QwenTtsVoice
        import soundfile as sf

        prompt_hash = self.file_hash(prompt_path)
        existing = db.query(QwenTtsVoice).filter(
            QwenTtsVoice.prompt_hash == prompt_hash,
            QwenTtsVoice.target_model == self.model_name,
        ).first()
        if existing:
            return existing.voice_id, False

        # 检查时长
        try:
            duration = sf.info(prompt_path).duration
            if duration < self.MIN_DURATION_S:
                raise ValueError(
                    f"Qwen-TTS enrollment 要求音频 ≥ {self.MIN_DURATION_S}s，当前 {duration:.1f}s"
                )
        except ValueError:
            raise
        except Exception:
            pass

        # base64 编码音频，通过 data URI 上传
        with open(prompt_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        data_uri = f"data:audio/wav;base64,{b64}"

        # prefix = awp + hash 前 6 位，≤10 chars，纯字母数字
        prefix = f"awp{prompt_hash[:6]}"

        payload = {
            "model": "voice-enrollment",
            "input": {
                "action": "create_voice",
                "target_model": self.model_name,
                "prefix": prefix,
                "url": data_uri,
                "language_hints": ["zh"],
            },
        }
        resp = requests.post(self.GATEWAY_URL, json=payload, headers=self.headers, timeout=120)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") and result["code"] != "200" and "output" not in result:
            raise RuntimeError(f"qwen-tts enrollment failed: {result}")

        voice_id = result["output"]["voice_id"]
        record = QwenTtsVoice(
            voice_id=voice_id,
            prompt_hash=prompt_hash,
            target_model=self.model_name,
            task_id=task_id,
        )
        db.add(record)
        db.commit()
        return voice_id, True

    def synthesize(self, text: str, voice_id: str, instructions: str = "") -> bytes:
        params: dict = {
            "voice": voice_id,
            "sample_rate": 24000,
            "format": "wav",
        }
        if instructions:
            params["instructions"] = instructions

        payload = {
            "model": self.model_name,
            "input": {"text": text},
            "parameters": params,
        }
        resp = requests.post(self.GATEWAY_URL, json=payload, headers=self.headers, timeout=120)
        resp.raise_for_status()
        result = resp.json()

        # 音频在 output.audio.url（OSS 链接）
        audio_url = result.get("output", {}).get("audio", {}).get("url", "")
        if not audio_url:
            raise RuntimeError(f"qwen-tts: no audio url in response: {result}")

        audio_resp = requests.get(audio_url, timeout=60)
        audio_resp.raise_for_status()
        return audio_resp.content


def _run_qwen_tts(task, model, items: list[dict], output_dir: str,
                  global_prompt: str | None, prompt_map: dict,
                  params: dict, extra: dict, db) -> ExecutorResult:
    input_data  = json.loads(task.input or "{}")
    api_key     = os.environ.get(extra.get("api_key_env", "TTS_AUTH"), "")
    model_name  = extra.get("model_name", "qwen-audio-3.0-tts-flash")
    instructions = params.get("instructions", "")

    adapter = QwenTtsAdapter(api_key=api_key, model_name=model_name)
    result  = ExecutorResult(total=len(items))

    # 按 prompt 音频分组，同一音频 enrollment 只做一次
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        prompt_path = _resolve_prompt(item, global_prompt, prompt_map)
        groups[prompt_path].append(item)

    ok = fail = 0

    for prompt_path, group_items in groups.items():
        try:
            voice_id, was_created = adapter.get_or_create_voice(prompt_path, task.id, db)
            action = "注册音色" if was_created else "复用音色"
            task.append_log(f"[qwen-tts {action}] {voice_id[-20:]}")
            db.commit()
        except Exception as e:
            for item in group_items:
                fail += 1
                result.items.append({"key": item.get("key", ""), "status": "failed",
                                     "error": f"enrollment: {e}"})
            task.append_log(f"[qwen-tts enrollment FAIL] {e}", "error")
            db.commit()
            continue

        for item in group_items:
            key      = item.get("key", "")
            out_path = os.path.join(output_dir, f"{key}.wav")

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[skip] {key}")
                db.commit()
                continue

            t0 = time.time()
            try:
                audio_bytes = adapter.synthesize(item.get("text", ""), voice_id, instructions)
                with open(out_path, "wb") as f:
                    f.write(audio_bytes)
                ok += 1
                result.items.append({"key": key, "status": "success",
                                     "audio_url": f"/storage/outputs/{task.id}/{key}.wav"})
                task.append_log(f"[{ok+fail}/{len(items)}] {key}: ok ({time.time()-t0:.1f}s)")
            except Exception as e:
                fail += 1
                result.items.append({"key": key, "status": "failed", "error": str(e)})
                task.append_log(f"[{ok+fail}/{len(items)}] {key}: FAIL - {e}", "error")
            db.commit()
            _update_parent_progress(db, input_data, ok, fail)
            time.sleep(0.2)

    result.ok = ok
    result.fail = fail
    return result


class APIExecutor(BaseExecutor):
    def execute(self, task, model, db: Session) -> ExecutorResult:
        input_data = json.loads(task.input or "{}")
        global_prompt = input_data.get("global_prompt_path")
        prompt_map = input_data.get("prompt_map", {})
        params = input_data.get("params", {})
        extra = json.loads(model.extra_config or "{}")

        output_dir = str(OUTPUTS_BASE / task.id)
        os.makedirs(output_dir, exist_ok=True)

        # 支持直接传 items 或从 jsonl_path 读取
        if "items" in input_data:
            items = input_data["items"]
        else:
            jsonl_path = input_data["jsonl_path"]
            items = _load_jsonl(jsonl_path)

        task.append_log(f"API 模型 {model.id}，共 {len(items)} 条")
        db.commit()

        if model.id == "elevenlabs":
            result = _run_elevenlabs(task, model, items, output_dir, global_prompt,
                                     prompt_map, params, extra, db)
        elif model.id == "mimo":
            result = _run_mimo(task, model, items, output_dir, global_prompt,
                               prompt_map, params, extra, db)
        elif model.id == "minimax":
            result = _run_minimax(task, model, items, output_dir, global_prompt,
                                  prompt_map, params, extra, db)
        elif model.id == "doubao":
            result = _run_doubao(task, model, items, output_dir, global_prompt,
                                 prompt_map, params, extra, db)
        elif model.id == "voxcpm2":
            result = _run_voxcpm2_grpc(task, model, items, output_dir, global_prompt,
                                       prompt_map, params, extra, db)
        elif model.id in ("qwen-tts-flash", "qwen-tts-plus"):
            result = _run_qwen_tts(task, model, items, output_dir,
                                   global_prompt, prompt_map, params, extra, db)
        else:
            raise ValueError(f"未知 API 模型: {model.id}")

        task.append_log(f"完成：{result.ok} 成功 / {result.fail} 失败")
        db.commit()
        return result


ExecutorFactory.register("api", APIExecutor)
