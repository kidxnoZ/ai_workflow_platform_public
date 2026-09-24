"""backend/executors/dataset_ingest_executor.py

DatasetIngestExecutor: 10-phase state machine for the audio dataset ingest pipeline.

Phase flow:
  start → dataset_preview → classify → classify_review → classify_confirm
        → metadata → pre_asr_preprocess → segment_confirm → asr
        → post_asr_preprocess → review_session → done

Key constraints:
- source_path used ONLY in _phase_start, never stored in task.input afterward.
- _integrity_check() called after every file-moving phase.
- cleanup iterates original_paths only (never os.listdir of current state).
- LLM called only at: classify (script gen), metadata (accent), post_asr_preprocess (richtext).
- experience appended to classification_experience.md on classify approval.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from sqlalchemy.orm import Session

from backend.executors.agent_base import AgentBaseExecutor
from backend.schemas.dataset_ingest import IntegrityResult
from backend.tools import classification, sandbox, audio_analysis, audio_preprocess
from backend.tools import rich_text_detector, pinyin_utils
from backend.tools import asr as asr_backend
from backend.config import ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL, ANTHROPIC_BASE_URL, STORAGE_DIR


class DatasetIngestExecutor(AgentBaseExecutor):

    # ── Phase dispatch ────────────────────────────────────────────────────────

    def _phase_start(self, task, input_data: dict, db: Session) -> None:
        """Copy source to output_path, archive integrity test, pre-flight checks,
        record INITIAL_COUNT and original_paths."""
        output_path = input_data["output_path"]
        source_type = input_data["source_type"]

        # 1. Copy or extract source
        if source_type == "server_path":
            source_path = input_data.get("source_path", "")
            if source_path:
                shutil.copytree(source_path, output_path, dirs_exist_ok=True)
        elif source_type == "upload":
            upload_path = input_data.get("upload_path", "")
            if upload_path:
                # Test archive integrity first
                try:
                    zf = zipfile.ZipFile(upload_path)
                except Exception as e:
                    self._sse_emit(task, db, "error", step="start",
                                   message=f"压缩包无法打开: {e}，请重新上传")
                    raise
                bad_file = zf.testzip()
                if bad_file is not None:
                    zf.close()
                    self._sse_emit(task, db, "error", step="start",
                                   message=f"压缩包损坏，损坏文件: {bad_file}，请重新上传")
                    raise ValueError(f"Corrupted archive: {bad_file}")
                zf.extractall(output_path)
                zf.close()

        # NOTE: source_path / upload_path are NOT stored in input_data after this point.

        # 2. Pre-flight checks
        preflight = {
            "ffprobe": shutil.which("ffprobe") is not None,
            "pypinyin": False,
        }
        try:
            import pypinyin  # noqa: F401
            preflight["pypinyin"] = True
        except ImportError:
            pass
        input_data["preflight"] = preflight

        # 3. Count initial audio files
        initial_count = classification.count_audio_files(output_path)
        input_data["initial_count"] = initial_count

        # 4. Record top-level paths (before any file movement)
        struct = classification.scan_directory(output_path)
        input_data["original_paths"] = struct.original_paths

        # 5. Keep source reference for re-copy on classification reject.
        # source_path/upload_path are already in input_data from the API — we
        # just make the intent explicit here. The LLM preamble never injects these.

        # 6. Emit step_start
        self._sse_emit(task, db, "step_start", step="start", label="初始化")

        # Commit
        self._save_phase_data(task, input_data, db)

        # Automatically advance to dataset_preview (no user confirmation needed for init)
        input_data["phase"] = "dataset_preview"
        self._save_phase_data(task, input_data, db)
        self._phase_dataset_preview(task, input_data, db)

    def _phase_dataset_preview(self, task, input_data: dict, db: Session) -> None:
        """Pause after data copy: let user browse directory structure and add a description.
        The description is stored in input_data["user_action"]["feedback"] for use in classify prompt.
        """
        output_path = input_data["output_path"]

        # Scan to show summary
        struct = classification.scan_directory(output_path)
        input_data["dataset_structure"] = struct.structure_type
        self._save_phase_data(task, input_data, db)

        self._sse_emit(task, db, "step_start", step="dataset_preview", label="预览数据集")
        self._sse_emit(task, db, "progress", step="dataset_preview",
                       label=f"数据集就绪：{struct.structure_type} 结构，{struct.total_audio_files} 个音频文件")

        self._sse_emit(task, db, "pause", step="dataset_preview",
                       payload={
                           "output_path": output_path,
                           "structure_type": struct.structure_type,
                           "total_audio_files": struct.total_audio_files,
                       })
        self._pause(task, input_data, "classify", db)

    def _phase_classify(self, task, input_data: dict, db: Session) -> None:
        """Detect dataset structure, load reference scripts + experience,
        call LLM (streaming) to generate classification script + merge candidates.
        _pause() -> classify_review."""
        output_path = input_data["output_path"]

        # Extract user description from dataset_preview action
        user_action = input_data.pop("user_action", {})
        user_dataset_description = user_action.get("feedback", "").strip()

        # 1. Emit step_start
        self._sse_emit(task, db, "step_start", step="classify", label="数据分类")

        # 2. Scan directory structure
        struct = classification.scan_directory(output_path)
        input_data["dataset_structure"] = struct.structure_type

        # 3. Load reference scripts + experience
        ref_scripts = classification.load_reference_scripts()
        experience = classification.load_classification_experience()

        # Build LLM context
        sample_info_lines = []
        for dir_name, samples in struct.sample_filenames.items():
            if samples:
                sample_info_lines.append(f"  {dir_name}: {samples[:5]}")

        # Build system prompt
        system_prompt = (
            "你是游戏语音数据集分类专家。以下是过往数据集的模式，供建立直觉，不可照搬关键词——"
            "每个数据集命名规则不同。拿不准时一律归入_special/unclassified/。"
        )

        experience_section = ""
        if experience:
            experience_section = (
                f"\n\n以下是过往数据集的模式，供建立直觉，"
                f"不可照搬关键词——每个数据集命名规则不同：\n{experience}"
            )

        user_prompt = f"""目录结构类型: {struct.structure_type}
输出路径: {output_path}
初始音频文件数: {struct.total_audio_files}

目录结构摘要与文件名样本:
{chr(10).join(sample_info_lines)}

参考分类脚本:
{ref_scripts}
{experience_section}

任务：生成一个 Python 分类脚本，将音频文件整理为 `{{spk_id}}/audio/` 结构，将多人/方言/无法分类的数据移至 `_special/{{multi_speaker|dialect|unclassified}}/`。

**输出格式要求（严格遵守）：**
第一部分：用中文写出你的分析推理——
- 目录命名规律是什么
- 哪些目录是单角色，判断依据
- 哪些是多说话人/场景，为什么
- 是否有需要合并的同角色变体
- 特殊情况说明

第二部分：用 ```python ... ``` 包裹完整分类脚本

强制规则：
1. 拿不准某个目录是单角色还是多说话人时，一律归入 `_special/unclassified/`
2. 脚本只能使用 shutil.move、os.makedirs、os.rmdir、shutil.rmtree，目标路径必须在 output_path 内
3. 脚本处理后 output_path 下每个角色目录必须有 audio/ 子目录
4. **必须用注入的 `original_paths` 列表迭代所有顶层目录**，每个元素是一个完整路径，必须全部处理——绝不能只取 `original_paths[0]`，也不能用 `os.listdir(output_path)` 自行扫描
5. cv_hierarchy 结构：`original_paths` 里每个元素是 CV 目录，CV 目录下的子目录是角色目录，角色目录下直接是音频文件——循环必须是 `for cv_path in original_paths: for role_dir in cv_path/子目录: 移动音频到 output_path/role_name/audio/`
6. 角色目录名应尽量保持与原始目录名一致，spk_id 直接是角色名，不要加数字后缀

在脚本末尾，以如下格式输出合并候选列表（如果有同角色不同名的目录需要合并）：
# MERGE_CANDIDATES: [{{"src_speaker": "...", "dst_speaker": "...", "reason": "..."}}]"""

        # Inject user's dataset description from the dataset_preview phase
        if user_dataset_description:
            user_prompt += f"\n\n用户对数据集的补充描述（来自人工浏览）：\n{user_dataset_description}"

        # 4. Call Anthropic API (streaming)
        full_response = ""
        try:
            import anthropic

            client_kwargs: dict = {}
            if ANTHROPIC_API_KEY:
                client_kwargs["api_key"] = ANTHROPIC_API_KEY
            elif ANTHROPIC_AUTH_TOKEN:
                client_kwargs["auth_token"] = ANTHROPIC_AUTH_TOKEN
            if ANTHROPIC_BASE_URL:
                client_kwargs["base_url"] = ANTHROPIC_BASE_URL

            client = anthropic.Anthropic(**client_kwargs, timeout=120.0)

            with client.messages.stream(
                model=ANTHROPIC_MODEL,
                max_tokens=8192,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                # Batch thinking events: accumulate tokens and flush every 80 chars
                # to avoid committing on every single token (SQLite lock contention)
                thinking_buf = ""
                for text in stream.text_stream:
                    full_response += text
                    thinking_buf += text
                    if len(thinking_buf) >= 10:
                        self._sse_emit(task, db, "thinking", content=thinking_buf)
                        thinking_buf = ""
                # Flush remaining buffer
                if thinking_buf:
                    self._sse_emit(task, db, "thinking", content=thinking_buf)
        except Exception as e:
            self._sse_emit(task, db, "error", step="classify",
                           message=f"LLM 调用失败: {e}")
            raise

        # 5. Parse generated script and merge candidates from response
        script, merge_candidates = self._parse_classify_response(full_response)
        input_data["classify_script"] = script
        input_data["merge_candidates"] = merge_candidates

        self._save_phase_data(task, input_data, db)

        # 6. Pause for review
        self._sse_emit(task, db, "pause", step="classify_review",
                       payload={"script_preview": script[:2000]})
        self._pause(task, input_data, "classify_review", db)

    def _phase_classify_review(self, task, input_data: dict, db: Session) -> None:
        """Execute script via 6-layer sandbox (dry-run first),
        generate _role_list.txt and dir_tree, run integrity check.

        Re-entry handling after sandbox_error pause:
        - user_action == "resubmit_script": run user-edited script through full sandbox
        - user_action == "sandbox_override": skip static analysis, run directly (dev mode)
        """
        output_path = input_data["output_path"]
        initial_count = input_data["initial_count"]

        # Handle re-entry from sandbox_error pause
        user_action = input_data.pop("user_action", {})
        action = user_action.get("action", "")

        if action == "resubmit_script":
            # User edited the script manually — overwrite and proceed through full sandbox
            edited = user_action.get("script", "").strip()
            if edited:
                input_data["classify_script"] = edited
                input_data.pop("sandbox_error", None)
                self._save_phase_data(task, input_data, db)
            self._sse_emit(task, db, "progress", step="classify_review",
                           label="使用人工修改脚本重新检查…")

        elif action == "sandbox_override":
            # Skip static analysis — run directly (Layer 4 _SafeShutil still active)
            script = input_data.get("classify_script", "")
            self._sse_emit(task, db, "progress", step="classify_review",
                           label="⚠️ 跳过沙箱静态检查，直接执行（开发模式）")
            config = sandbox.SandboxConfig(
                allowed_dest_paths=[output_path],
                audit_log_dir=os.path.join(output_path, ".ingest_audit"),
                task_id=task.id,
            )
            exec_result = sandbox.run_in_subprocess(script, config, dry_run=False)
            if not exec_result.success:
                self._sse_emit(task, db, "error", step="classify_review",
                               message=f"脚本执行失败: {exec_result.stderr}")
                return
            # Skip to post-execution steps
            role_list = classification.generate_role_list(output_path)
            dir_tree = classification.generate_dir_tree(output_path)
            input_data["dir_tree"] = dir_tree
            integrity = self._integrity_check(output_path, initial_count)
            if not integrity.ok:
                self._sse_emit(task, db, "error", step="classify_review",
                               message=f"完整性校验失败: {integrity.detail}")
            self._save_phase_data(task, input_data, db)
            self._sse_emit(task, db, "pause", step="classify_confirm",
                           payload={
                               "dir_tree": dir_tree,
                               "role_list": role_list,
                               "integrity": integrity.model_dump(),
                               "merge_candidates": input_data.get("merge_candidates", []),
                               "script_preview": script[:2000],
                           })
            self._pause(task, input_data, "classify_confirm", db)
            return

        script = input_data.get("classify_script", "")

        # 1. Emit step_start — use different label on retry
        step_label = "验证并执行修改后的脚本" if action == "resubmit_script" else "执行分类脚本"
        self._sse_emit(task, db, "step_start", step="classify_review", label=step_label)

        # 2. Build SandboxConfig
        audit_log_dir = os.path.join(output_path, ".ingest_audit")
        config = sandbox.SandboxConfig(
            allowed_dest_paths=[output_path],
            audit_log_dir=audit_log_dir,
            task_id=task.id,
        )

        # 3. Build speaker map — skip on resubmit (already built, no files moved yet)
        if action != "resubmit_script":
            structure_type = input_data.get("dataset_structure", "flat")
            speaker_map = self._build_speaker_map(output_path, structure_type=structure_type)
            map_path = Path(output_path) / "_speaker_map.json"
            map_path.write_text(
                json.dumps(speaker_map, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            input_data["speaker_map"] = speaker_map
            self._save_phase_data(task, input_data, db)
        self._sse_emit(task, db, "progress", step="classify_review",
                       label=f"已保存说话人映射：{len(speaker_map)} 个原始目录")

        # 4. Inject preamble: define variables the LLM script may reference
        #    LLMs often use executor-scope variables (initial_count, output_path, original_paths)
        #    that aren't defined in the standalone script. Predefine them to prevent NameError.
        original_paths_repr = repr(input_data.get("original_paths", []))
        preamble = f"""import os
import shutil
from pathlib import Path

# --- Variables injected by executor (safe to use in this script) ---
output_path = {repr(output_path)}
initial_count = {initial_count}
original_paths = {original_paths_repr}
# ------------------------------------------------------------------

"""
        def _with_preamble(raw_script: str) -> str:
            """Always prepend preamble — Python allows variable redefinition, no harm."""
            return preamble + raw_script

        script = _with_preamble(script)
        input_data["classify_script"] = script
        self._save_phase_data(task, input_data, db)

        # 5. Dry-run with self-healing retry loop (max 3 attempts: 1 original + 2 auto-fixes)
        MAX_SANDBOX_RETRIES = 2
        sandbox_error = None
        dry_result = None

        for attempt in range(MAX_SANDBOX_RETRIES + 1):
            dry_result = classification.execute_classification_script(script, config, dry_run=True)

            if dry_result.success:
                sandbox_error = None
                break

            sandbox_error = dry_result.stderr

            if attempt < MAX_SANDBOX_RETRIES:
                # Show the actual error so the user can see what failed
                self._sse_emit(task, db, "error", step="classify_review",
                               message=f"沙箱检查失败（第{attempt+1}次）: {sandbox_error}")
                # Mark the LLM fix attempt as a visible step
                self._sse_emit(task, db, "step_start", step="sandbox_fix",
                               label=f"LLM 自动修复脚本（第{attempt+1}/{MAX_SANDBOX_RETRIES}次）")
                script = self._fix_script_with_llm(script, sandbox_error, input_data, task, db)
                if script is None:
                    self._sse_emit(task, db, "error", step="sandbox_fix",
                                   message="LLM 修复脚本失败，无法继续")
                    return
                self._sse_emit(task, db, "step_done", step="sandbox_fix",
                               summary="脚本已修复，重新进行沙箱检查")
                # Re-apply preamble to the LLM-fixed script
                script = _with_preamble(script)
                input_data["classify_script"] = script
                self._save_phase_data(task, input_data, db)
            else:
                # All retries exhausted — show final error and pause for user
                self._sse_emit(task, db, "error", step="classify_review",
                               message=f"沙箱检查失败（最终）: {sandbox_error}")
                self._sse_emit(task, db, "error", step="classify_review",
                               message=f"已自动修复 {MAX_SANDBOX_RETRIES} 次仍未通过，请在右侧手动修改脚本")
                input_data["sandbox_error"] = sandbox_error
                input_data["classify_script"] = script
                self._save_phase_data(task, input_data, db)
                self._sse_emit(task, db, "pause", step="sandbox_error",
                               payload={
                                   "error": sandbox_error,
                                   "script": script,
                               })
                self._pause(task, input_data, "classify_review", db)
                return

        if sandbox_error:
            return

        # 5. Emit ops plan (from last successful dry-run)
        self._sse_emit(task, db, "progress", step="classify_review",
                       label=f"操作计划: {len(dry_result.ops_log)} 个操作",
                       payload={"ops": dry_result.ops_log})

        # 6. Execute for real
        exec_result = sandbox.run_in_subprocess(script, config, dry_run=False)
        if not exec_result.success:
            self._sse_emit(task, db, "error", step="classify_review",
                           message=f"分类脚本执行失败: {exec_result.stderr}")
            return
        self._save_phase_data(task, input_data, db)

        # 6.5 Harness normalisation — guarantee {role}/audio/ regardless of what the script did
        moved = self._normalize_output_structure(output_path)
        if moved > 0:
            self._sse_emit(task, db, "progress", step="classify_review",
                           label=f"输出结构已标准化（{moved} 个文件整理到 audio/ 子目录）")

        # 7. Generate role list
        role_list = classification.generate_role_list(output_path)

        # 8. Generate dir tree
        dir_tree = classification.generate_dir_tree(output_path)
        input_data["dir_tree"] = dir_tree

        # 9. Integrity check
        integrity = self._integrity_check(output_path, initial_count)
        if not integrity.ok:
            self._sse_emit(task, db, "error", step="classify_review",
                           message=f"完整性校验失败: {integrity.detail}")
            self._save_phase_data(task, input_data, db)
            return

        self._sse_emit(task, db, "progress", step="classify_review",
                       label=f"完整性校验通过: {integrity.detail}")

        self._save_phase_data(task, input_data, db)

        # 10. Pause for user confirmation
        self._sse_emit(task, db, "pause", step="classify_confirm",
                       payload={
                           "dir_tree": dir_tree,
                           "role_list": role_list,
                           "integrity": integrity.model_dump(),
                           "merge_candidates": input_data.get("merge_candidates", []),
                           "script_preview": input_data.get("classify_script", "")[:2000],
                       })
        self._pause(task, input_data, "classify_confirm", db)

    def _phase_classify_confirm(self, task, input_data: dict, db: Session) -> None:
        """On approve: apply confirmed MERGE_TASKS, append experience entry, advance to metadata.
        On reject: append feedback to LLM context, return to classify."""
        output_path = input_data["output_path"]
        user_action = input_data.get("user_action", {})

        action = user_action.get("action", "approve")

        if action == "approve":
            # Apply confirmed merges
            confirmed_merges = user_action.get("confirmed_merges",
                                                input_data.get("merge_candidates", []))
            if confirmed_merges:
                # Build config for merge operations
                audit_log_dir = os.path.join(output_path, ".ingest_audit")
                merge_config = sandbox.SandboxConfig(
                    allowed_dest_paths=[output_path],
                    audit_log_dir=audit_log_dir,
                    task_id=task.id,
                )
                classification.apply_merge_tasks(confirmed_merges, output_path, merge_config)

            # Build and append experience entry — only if user explicitly approved
            if user_action.get("save_experience", False):
                experience_entry = self._build_experience_entry(input_data)
                classification.append_experience_entry(experience_entry)
                self._sse_emit(task, db, "step_done", step="classify",
                               summary="分类完成，经验已写入")
            else:
                self._sse_emit(task, db, "step_done", step="classify",
                               summary="分类完成")

            # Advance to metadata
            input_data["phase"] = "metadata"
            self._save_phase_data(task, input_data, db)
            # Re-dispatch to _phase_metadata
            self._phase_metadata(task, input_data, db)

        elif action == "reject":
            feedback = user_action.get("feedback", "")
            classify_feedback = input_data.get("classify_feedback", "")
            if classify_feedback:
                classify_feedback += "\n" + feedback
            else:
                classify_feedback = feedback
            input_data["classify_feedback"] = classify_feedback

            # Return to classify phase: delete output_path and re-copy from source.
            # This is simpler and more reliable than reversing individual file moves.
            self._sse_emit(task, db, "progress", step="classify_review",
                           label="正在清空输出目录，从源数据重新开始…")
            self._reset_output_from_source(output_path, input_data, task, db)

            input_data["phase"] = "classify"
            self._save_phase_data(task, input_data, db)
            self._phase_classify(task, input_data, db)

    def _phase_metadata(self, task, input_data: dict, db: Session) -> None:
        """Parallel metadata.json generation (ThreadPoolExecutor, max_workers=8).
        ffprobe with codec_type filter; duration = round(sec/3600, 2).
        Post-phase: warn on sample_rate=0 / duration=0 / missing audio/."""
        output_path = input_data["output_path"]
        initial_count = input_data["initial_count"]

        # 0. Load speaker_map from input_data or from disk
        speaker_map = input_data.get("speaker_map", {})
        map_path = Path(output_path) / "_speaker_map.json"
        if not speaker_map and map_path.exists():
            try:
                speaker_map = json.loads(map_path.read_text(encoding="utf-8"))
            except Exception:
                speaker_map = {}

        # 1. Emit step_start
        self._sse_emit(task, db, "step_start", step="metadata",
                       label="生成 metadata.json")

        # 2. Collect speaker dirs
        spk_dirs = self._collect_speaker_dirs(output_path)
        if not spk_dirs:
            self._sse_emit(task, db, "step_done", step="metadata",
                           summary="没有找到角色目录")
            return

        total = len(spk_dirs)
        source_label = input_data.get("source_label", "")
        copyright_val = input_data.get("copyright", "")
        domain = input_data.get("domain", "")
        language = input_data.get("language", "中文")
        version = input_data.get("version", "1.0")

        errors: list[str] = []
        completed = 0

        def _process_speaker(spk_dir: str) -> dict | None:
            """Generate metadata.json for one speaker directory. Returns a
            warning dict on issues, or None on success.
            NOTE: must NOT call _sse_emit or db.commit — this runs in a thread pool
            and SQLAlchemy sessions are not thread-safe."""
            try:
                spk_path = Path(spk_dir)
                audio_dir = self._audio_dir_of(spk_dir)
                if not audio_dir.is_dir():
                    return {"speaker": spk_path.name, "warning": "missing audio dir"}

                # Collect audio files
                audio_exts = {".wav", ".mp3", ".flac"}
                audio_files = sorted(
                    p for p in audio_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in audio_exts
                )
                count = len(audio_files)

                if count == 0:
                    return {"speaker": spk_path.name, "warning": "no audio files in audio/"}

                # ffprobe on first file for sample_rate
                first_file = audio_files[0]
                sample_rate, fmt = self._ffprobe_audio(str(first_file))

                # Sum durations of all files
                total_sec = 0.0
                for af in audio_files:
                    dur = self._ffprobe_duration(str(af))
                    total_sec += dur

                # duration = round(total_sec / 3600, 2)
                duration = round(total_sec / 3600, 2)

                # speaker name from directory — check speaker_map first
                spk_name = spk_path.name
                original_entry = speaker_map.get(spk_name, {})
                speaker_name = original_entry.get("speaker_name", spk_name)
                speaker = (
                    pinyin_utils.to_pinyin(speaker_name)
                    if pinyin_utils.is_chinese(speaker_name)
                    else speaker_name
                )

                # Build metadata dict (16 fields per §5.1)
                metadata = {
                    "speaker": speaker,
                    "speaker_name": speaker_name,
                    "age": "",
                    "gender": "",
                    "sample_rate": sample_rate,
                    "format": fmt,
                    "language": language,
                    "accent": "mandarin",
                    "root_dir": str(spk_path),
                    "source": source_label,
                    "count": count,
                    "duration": duration,
                    "actor": original_entry.get("actor", ""),
                    "copyright": copyright_val,
                    "domain": domain,
                    "description": "",
                    "version": version,
                }

                # Write metadata.json
                metadata_path = spk_path / "metadata.json"
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return None  # success, progress emitted by main thread

            except Exception as e:
                return {"speaker": Path(spk_dir).name if spk_dir else "?", "warning": str(e)}

        # 3. Run in ThreadPoolExecutor (max_workers=8)
        # Progress is emitted in the MAIN THREAD after each future completes
        # to avoid SQLAlchemy session thread-safety issues.
        completed = 0
        # Process speakers serially — ThreadPoolExecutor caused SQLAlchemy
        # session state corruption (session not thread-safe across concurrent commits).
        # ffprobe is I/O bound but the session safety is more important here.
        for i, spk_dir in enumerate(spk_dirs, 1):
            warning = _process_speaker(spk_dir)
            self._sse_emit(task, db, "progress",
                           current=i, total=total,
                           step="metadata",
                           label=f"已处理 {i}/{total}")
            if warning:
                errors.append(warning)

        # 4. Post-phase warnings
        for err in errors:
            self._sse_emit(task, db, "error", step="metadata",
                           message=f"告警: {err['speaker']} - {err['warning']}")

        # 5. Integrity check
        integrity = self._integrity_check(output_path, initial_count)
        if not integrity.ok:
            self._sse_emit(task, db, "error", step="metadata",
                           message=f"完整性校验失败: {integrity.detail}")

        # 6. Emit step_done
        self._sse_emit(task, db, "step_done", step="metadata",
                       summary=f"生成了 {total} 个 metadata.json，{len(errors)} 个告警")

        self._save_phase_data(task, input_data, db)

        # Automatically advance to pre_asr_preprocess
        input_data["phase"] = "pre_asr_preprocess"
        self._save_phase_data(task, input_data, db)
        self._phase_pre_asr_preprocess(task, input_data, db)

    def _phase_asr(self, task, input_data: dict, db: Session) -> None:
        """Run ASR on all audio files, build initial data.jsonl per speaker.
        Post-phase: verify data.jsonl count == role count, total rows == audio files."""
        output_path = input_data["output_path"]

        # 1. Emit step_start
        self._sse_emit(task, db, "step_start", step="asr", label="ASR 转录")

        # 2. Walk speaker dirs
        spk_dirs = self._collect_speaker_dirs(output_path)
        total_speakers = len(spk_dirs)
        total_audio_files = 0
        total_rows = 0
        completed = 0

        for spk_dir in spk_dirs:
            spk_path = Path(spk_dir)
            speaker_name = spk_path.name
            speaker = (
                pinyin_utils.to_pinyin(speaker_name)
                if pinyin_utils.is_chinese(speaker_name)
                else speaker_name
            )
            audio_dir = self._audio_dir_of(spk_dir)

            if not audio_dir.is_dir():
                completed += 1
                continue

            audio_exts = {".wav", ".mp3", ".flac"}
            audio_files = sorted(
                p for p in audio_dir.iterdir()
                if p.is_file() and p.suffix.lower() in audio_exts
            )

            if not audio_files:
                completed += 1
                continue

            records = []
            for af in audio_files:
                try:
                    asr_text = asr_backend.transcribe(str(af))
                except Exception as e:
                    asr_text = f"[ASR_ERROR: {e}]"

                # Get duration via ffprobe
                duration_sec = self._ffprobe_duration(str(af))

                # Build data.jsonl record
                record = {
                    "key": af.stem,
                    "path": f"audio/{af.name}",
                    "text": asr_text,
                    "rich_text": "",
                    "emotion": "",
                    "speaker": speaker,
                    "duration": duration_sec,
                    "audio_quality": 0,
                    "rich_text_level": 0,
                    "too_short": 0,
                    "flags": [],
                }
                records.append(record)

            # Write data.jsonl
            jsonl_path = spk_path / "data.jsonl"
            with open(jsonl_path, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            total_audio_files += len(audio_files)
            total_rows += len(records)
            completed += 1

            self._sse_emit(task, db, "progress",
                           current=completed, total=total_speakers,
                           step="asr",
                           label=f"ASR {speaker_name}")

        # 3. Verify: data.jsonl count == role count, total rows == audio files
        jsonl_count = 0
        for spk_dir in spk_dirs:
            jsonl_path = Path(spk_dir) / "data.jsonl"
            if jsonl_path.is_file():
                jsonl_count += 1

        if jsonl_count != len(spk_dirs):
            self._sse_emit(task, db, "error", step="asr",
                           message=f"data.jsonl 文件数 ({jsonl_count}) != 角色目录数 ({len(spk_dirs)})")

        if total_rows != total_audio_files:
            self._sse_emit(task, db, "error", step="asr",
                           message=f"data.jsonl 总行数 ({total_rows}) != 音频文件总数 ({total_audio_files})")

        # 4. Emit step_done
        self._sse_emit(task, db, "step_done", step="asr",
                       summary=f"完成 {total_speakers} 个说话人，{total_rows} 条记录")

        self._save_phase_data(task, input_data, db)

        # Automatically advance to post_asr_preprocess
        input_data["phase"] = "post_asr_preprocess"
        self._save_phase_data(task, input_data, db)
        self._phase_post_asr_preprocess(task, input_data, db)

    def _phase_pre_asr_preprocess(self, task, input_data: dict, db: Session) -> None:
        """Pre-ASR audio preprocessing: silence_trim only.
        Analyze → cluster → preview → pause for user confirmation → execute.
        After execution, advance to segment_confirm."""
        output_path = input_data["output_path"]
        initial_count = input_data.get("initial_count", 0)
        steps = input_data.get("steps", [])

        pre_steps = ["silence_trim"]
        enabled_steps = [s for s in steps if s in pre_steps]

        if not enabled_steps:
            # No silence_trim, skip to segment_confirm
            input_data["phase"] = "segment_confirm"
            self._save_phase_data(task, input_data, db)
            self._phase_segment_confirm(task, input_data, db)
            return

        spk_dirs = self._collect_speaker_dirs(output_path)

        # Check if resuming from a preview confirmation
        pending_step = input_data.pop("preprocess_pending_step", None)
        step_index = input_data.pop("preprocess_step_index", 0)

        if pending_step is not None:
            user_action = input_data.get("user_action", {})

            if pending_step == "silence_trim_review":
                # User confirmed post-execution review → just advance
                self._sse_emit(task, db, "step_done", step="silence_trim",
                               summary="首尾静音替换完成（已确认效果）")

            elif user_action.get("action") == "approve":
                self._execute_audio_step(pending_step, task, input_data, spk_dirs, db)
                # integrity check after silence_trim
                if pending_step == "silence_trim":
                    integrity = self._integrity_check(output_path, initial_count)
                    if not integrity.ok:
                        self._sse_emit(task, db, "error", step=pending_step,
                                       message=f"完整性校验失败（{pending_step}后）: {integrity.detail}")

                if pending_step == "silence_trim":
                    # Special: pause for post-execution before/after review
                    review_payload = self._build_silence_trim_review(task, input_data, db)
                    if review_payload:
                        input_data["preprocess_pending_step"] = "silence_trim_review"
                        input_data["preprocess_step_index"] = step_index
                        self._save_phase_data(task, input_data, db)
                        self._sse_emit(task, db, "pause", step="silence_trim_review",
                                       payload=review_payload)
                        self._pause(task, input_data, "pre_asr_preprocess", db)
                        return
                    else:
                        self._sse_emit(task, db, "step_done", step=pending_step,
                                       summary=f"{pending_step} 完成")
                else:
                    self._sse_emit(task, db, "step_done", step=pending_step,
                                   summary=f"{pending_step} 完成")
            else:
                self._sse_emit(task, db, "step_done", step=pending_step,
                               summary=f"{pending_step} 已跳过（用户拒绝参数）")

            # Move to next step
            step_index = step_index + 1

        # Process remaining steps
        for i in range(step_index, len(enabled_steps)):
            step_name = enabled_steps[i]

            # Audio modification step: analyze → cluster → preview → pause
            self._sse_emit(task, db, "step_start", step=step_name,
                           label=f"音频预处理: {step_name}")

            # 1. Analyze each speaker
            speaker_params_list = []
            for spk_dir in spk_dirs:
                try:
                    params = audio_analysis.analyze_speaker_params(spk_dir, sample_count=2)
                    speaker_params_list.append(params)
                except ValueError:
                    continue

            if not speaker_params_list:
                self._sse_emit(task, db, "step_done", step=step_name,
                               summary=f"无有效角色，跳过 {step_name}")
                continue

            # 2. Cluster
            groups = audio_analysis.cluster_speakers_by_params(speaker_params_list)

            # 3. Process sample files to produce before/after previews
            preview_cache_root = (
                STORAGE_DIR / "ingest_sessions" / task.id / "preview_cache"
            )
            preview_groups = []
            for group in groups:
                sample_paths = audio_analysis.sample_preview_files(group, output_path, count=5)
                params = {
                    "threshold_db": group.representative_params.threshold_db,
                    "pause_threshold_db": group.representative_params.pause_threshold_db,
                    "peak_db": group.representative_params.peak_db,
                    "mean_db": group.representative_params.mean_db,
                    "sample_rate": group.representative_params.sample_rate,
                }
                cache_dir = preview_cache_root / f"{step_name}_{group.group_id}"
                cache_dir.mkdir(parents=True, exist_ok=True)

                processed_samples = []
                auto_thresholds: list[float] = []
                for sample_path in sample_paths:
                    try:
                        key = Path(sample_path).stem
                        before_path = cache_dir / f"before_{key}.wav"
                        after_path = cache_dir / f"after_{key}.wav"

                        shutil.copy2(sample_path, str(before_path))

                        # silence_trim: two-window transition preview
                        head_envelope: list[float] = []
                        tail_envelope: list[float] = []
                        head_transition_pos: int = 0
                        tail_transition_pos: int = 0
                        segment_url: str = ""
                        tail_segment_url: str = ""
                        if step_name == "silence_trim":
                            try:
                                tp = audio_analysis.compute_threshold_preview(
                                    sample_path,
                                    estimated_threshold_db=params["threshold_db"],
                                )
                                head_envelope = tp["head_envelope"]
                                tail_envelope = tp["tail_envelope"]
                                head_transition_pos = tp["head_transition_pos"]
                                tail_transition_pos = tp["tail_transition_pos"]
                                auto_thresholds.append(tp["auto_threshold_db"])
                                from pydub import AudioSegment as _AS
                                _audio = _AS.from_file(sample_path)
                                # Export head window
                                _h_start = tp.get("window_start_ms", 0)
                                _h_dur = tp.get("window_duration_ms", 500)
                                seg_path = cache_dir / f"seg_{key}.wav"
                                _audio[_h_start:_h_start + _h_dur].export(str(seg_path), format="wav")
                                segment_url = f"{base}?path=ingest_sessions/{task.id}/preview_cache/{step_name}_{group.group_id}/seg_{key}.wav"
                                # Export tail window
                                _t_start = tp.get("tail_window_start_ms", 0)
                                _t_dur = tp.get("tail_window_duration_ms", 500)
                                seg_tail_path = cache_dir / f"seg_tail_{key}.wav"
                                _audio[_t_start:_t_start + _t_dur].export(str(seg_tail_path), format="wav")
                                tail_segment_url = f"{base}?path=ingest_sessions/{task.id}/preview_cache/{step_name}_{group.group_id}/seg_tail_{key}.wav"
                            except Exception:
                                head_envelope = []
                                tail_envelope = []
                                head_transition_pos = 0
                                tail_transition_pos = 0

                        # Process to produce "after"
                        self._run_preview_step(
                            step_name, sample_path, str(after_path),
                            str(cache_dir), params,
                        )

                        # Build relative URL paths for the API
                        base = f"/api/dataset-ingest/{task.id}/audio"
                        processed_samples.append({
                            "key": key,
                            "speaker_id": group.group_id,
                            "before_url": f"{base}?path=ingest_sessions/{task.id}/preview_cache/{step_name}_{group.group_id}/before_{key}.wav",
                            "after_url":  f"{base}?path=ingest_sessions/{task.id}/preview_cache/{step_name}_{group.group_id}/after_{key}.wav",
                            "duration_sec": 0.0,
                            "head_envelope": head_envelope,
                            "tail_envelope": tail_envelope,
                            "head_transition_pos": head_transition_pos,
                            "tail_transition_pos": tail_transition_pos,
                            "segment_url": segment_url,
                            "tail_segment_url": tail_segment_url,
                        })
                    except Exception as e:
                        self._sse_emit(task, db, "error", step=step_name,
                                       message=f"样本处理失败 {Path(sample_path).name}: {e}")

                # For silence_trim: update group threshold with auto-detected value
                if step_name == "silence_trim" and auto_thresholds:
                    avg_thresh = round(float(sum(auto_thresholds) / len(auto_thresholds)), 1)
                    params["threshold_db"] = avg_thresh
                    params["pause_threshold_db"] = avg_thresh

                preview_groups.append({
                    "group_id": group.group_id,
                    "speaker_ids": group.speaker_ids,
                    "representative_params": params,
                    "samples": processed_samples,
                })

            # Store for resume
            input_data[f"{step_name}_preview"] = preview_groups
            input_data["preprocess_pending_step"] = step_name
            input_data["preprocess_step_index"] = i
            self._save_phase_data(task, input_data, db)

            # 4. Pause for user confirmation
            self._sse_emit(task, db, "pause", step=f"preview_{step_name}",
                           payload={"groups": preview_groups})
            self._pause(task, input_data, "pre_asr_preprocess", db)
            return  # Wait for user confirmation; on resume, re-enters this method

        # All enabled steps complete — advance to segment_confirm
        input_data["phase"] = "segment_confirm"
        self._save_phase_data(task, input_data, db)
        self._phase_segment_confirm(task, input_data, db)

    def _phase_segment_confirm(self, task, input_data: dict, db: Session) -> None:
        """If 'segment' is enabled, pause for threshold confirmation then execute.
        segment splits files at silence gaps > threshold_sec (default 2.0s).
        No per-speaker analysis or clustering needed.
        After execution (or skip), advances to _phase_asr.
        """
        steps = input_data.get("steps", [])
        output_path = input_data["output_path"]
        initial_count = input_data.get("initial_count", 0)

        if "segment" not in steps:
            # Skip to ASR
            input_data["phase"] = "asr"
            self._save_phase_data(task, input_data, db)
            self._phase_asr(task, input_data, db)
            return

        user_action = input_data.pop("user_action", {})

        # Check if resuming from segment confirmation pause
        if user_action:
            # Case 1: resuming from segment_done (result review)
            if input_data.pop("segment_done", False):
                self._sse_emit(task, db, "step_done", step="segment", summary="切句完成")
                input_data["phase"] = "asr"
                self._save_phase_data(task, input_data, db)
                self._phase_asr(task, input_data, db)
                return

            # Case 2: resuming from segment_confirm (threshold input)
            if user_action.get("action") == "approve":
                try:
                    feedback = json.loads(user_action.get("feedback") or "{}")
                    threshold_sec = float(feedback.get("threshold_sec", 2.0))
                except (json.JSONDecodeError, ValueError, TypeError):
                    threshold_sec = 2.0

                self._sse_emit(task, db, "step_start", step="segment", label=f"切句（停顿 > {threshold_sec}s）")
                spk_dirs = self._collect_speaker_dirs(output_path)
                total_scanned = total_split = total_segments = 0
                for spk_dir in spk_dirs:
                    audio_dir = self._audio_dir_of(spk_dir)
                    if audio_dir.is_dir():
                        audio_exts = {".wav", ".mp3", ".flac"}
                        audio_files = sorted(
                            p for p in audio_dir.iterdir()
                            if p.is_file() and p.suffix.lower() in audio_exts
                        )
                        if audio_files:
                            stats = self._execute_segment_step(
                                spk_dir, audio_files, output_path, task, db,
                                silence_threshold_sec=threshold_sec,
                            )
                            total_scanned += stats["files_scanned"]
                            total_split += stats["files_split"]
                            total_segments += stats["segments_created"]

                integrity = self._integrity_check(output_path, initial_count)
                if not integrity.ok:
                    self._sse_emit(task, db, "error", step="segment",
                                   message=f"完整性校验失败（切句后）: {integrity.detail}")

                # Pause to show results
                summary_msg = (
                    f"共扫描 {total_scanned} 个文件，切分了 {total_split} 个，"
                    f"新增 {total_segments - total_split} 条（总 {total_scanned - total_split + total_segments} 条）"
                    if total_split > 0 else f"共扫描 {total_scanned} 个文件，未检测到需要切分的停顿"
                )
                input_data["phase"] = "segment_confirm"
                input_data["segment_done"] = True
                self._save_phase_data(task, input_data, db)
                self._sse_emit(task, db, "pause", step="segment_done",
                               payload={
                                   "files_scanned": total_scanned,
                                   "files_split": total_split,
                                   "segments_created": total_segments,
                                   "threshold_sec": threshold_sec,
                                   "summary": summary_msg,
                               })
                self._pause(task, input_data, "segment_confirm", db)
                return
            else:
                self._sse_emit(task, db, "step_done", step="segment", summary="切句已跳过")

            # Advance to ASR
            input_data["phase"] = "asr"
            self._save_phase_data(task, input_data, db)
            self._phase_asr(task, input_data, db)
            return

        # First entry: pause for threshold confirmation
        threshold_sec = input_data.get("segment_threshold_sec", 2.0)
        input_data["phase"] = "segment_confirm"
        self._save_phase_data(task, input_data, db)
        self._sse_emit(task, db, "pause", step="segment_confirm",
                       payload={"threshold_sec": threshold_sec})
        self._pause(task, input_data, "segment_confirm", db)

    def _phase_post_asr_preprocess(self, task, input_data: dict, db: Session) -> None:
        """For each enabled step in input_data['steps']:
          1. analyze_speaker_params (sample 1-3 files per speaker)
          2. cluster_speakers_by_params → ParamGroups
          3. _pause() with preview samples per group
          4. on approve: batch process full group
        Steps: pause_compress, gain, flag_short, flag_richtext."""
        output_path = input_data["output_path"]
        initial_count = input_data.get("initial_count", 0)  # needed for integrity checks
        steps = input_data.get("steps", [])

        # Filter to only the post-ASR audio preprocess steps
        preprocess_steps = ["pause_compress", "gain", "flag_short", "flag_richtext"]
        enabled_steps = [s for s in steps if s in preprocess_steps]

        if not enabled_steps:
            # No audio steps to run, go straight to review
            input_data["phase"] = "review_session"
            self._save_phase_data(task, input_data, db)
            self._phase_review_session(task, input_data, db)
            return

        spk_dirs = self._collect_speaker_dirs(output_path)

        # Check if resuming from a preview confirmation
        pending_step = input_data.pop("preprocess_pending_step", None)
        step_index = input_data.pop("preprocess_step_index", 0)

        if pending_step is not None:
            user_action = input_data.get("user_action", {})

            # Resuming from a post-execution review OR flag result confirmation
            if pending_step.endswith("_review") or pending_step.endswith("_result"):
                base_step = pending_step.rsplit("_", 1)[0] if pending_step.endswith("_result") else pending_step[:-7]
                self._sse_emit(task, db, "step_done", step=base_step,
                               summary=f"{base_step} 完成（已确认）")

            elif user_action.get("action") == "approve":
                self._execute_audio_step(pending_step, task, input_data, spk_dirs, db)
                if pending_step in ("pause_compress", "gain"):
                    integrity = self._integrity_check(output_path, initial_count)
                    if not integrity.ok:
                        self._sse_emit(task, db, "error", step=pending_step,
                                       message=f"完整性校验失败（{pending_step}后）: {integrity.detail}")

                # Post-execution review for pause_compress and gain
                if pending_step in ("pause_compress", "gain"):
                    review_payload = self._build_post_execution_review(pending_step, task, input_data, db)
                    if review_payload:
                        input_data["preprocess_pending_step"] = f"{pending_step}_review"
                        input_data["preprocess_step_index"] = step_index
                        self._save_phase_data(task, input_data, db)
                        self._sse_emit(task, db, "pause", step=f"{pending_step}_review",
                                       payload=review_payload)
                        self._pause(task, input_data, "post_asr_preprocess", db)
                        return
                    else:
                        self._sse_emit(task, db, "step_done", step=pending_step,
                                       summary=f"{pending_step} 完成")
                else:
                    self._sse_emit(task, db, "step_done", step=pending_step,
                                   summary=f"{pending_step} 完成")
            else:
                self._sse_emit(task, db, "step_done", step=pending_step,
                               summary=f"{pending_step} 已跳过（用户拒绝参数）")

            step_index = step_index + 1

        # Process remaining steps
        for i in range(step_index, len(enabled_steps)):
            step_name = enabled_steps[i]

            if step_name in ("flag_short", "flag_richtext"):
                self._sse_emit(task, db, "step_start", step=step_name,
                               label=f"{'过短标注' if step_name == 'flag_short' else '富文本标注'}")
                if step_name == "flag_short":
                    result = self._execute_flag_short(task, input_data, spk_dirs, db)
                else:
                    result = self._execute_flag_richtext(task, input_data, spk_dirs, db)

                label = "过短片段" if step_name == "flag_short" else "富文本"
                input_data["preprocess_pending_step"] = f"{step_name}_result"
                input_data["preprocess_step_index"] = i
                self._save_phase_data(task, input_data, db)
                self._sse_emit(task, db, "pause", step=f"{step_name}_result",
                               payload={
                                   "step": step_name,
                                   "total_flagged": result["total_flagged"],
                                   "samples": result["samples"],
                                   "label": label,
                               })
                self._pause(task, input_data, "post_asr_preprocess", db)
                return

            # Audio modification steps: analyze → cluster → preview → pause → execute
            self._sse_emit(task, db, "step_start", step=step_name,
                           label=f"音频预处理: {step_name}")

            # 1. Analyze each speaker
            speaker_params_list = []
            for spk_dir in spk_dirs:
                try:
                    params = audio_analysis.analyze_speaker_params(spk_dir, sample_count=2)
                    speaker_params_list.append(params)
                except ValueError:
                    continue

            if not speaker_params_list:
                self._sse_emit(task, db, "step_done", step=step_name,
                               summary=f"无有效角色，跳过 {step_name}")
                continue

            # 2. Cluster
            groups = audio_analysis.cluster_speakers_by_params(speaker_params_list)

            # 3. Process sample files to produce before/after previews
            preview_cache_root = (
                STORAGE_DIR / "ingest_sessions" / task.id / "preview_cache"
            )
            preview_groups = []
            for group in groups:
                sample_paths = audio_analysis.sample_preview_files(group, output_path, count=5)
                params = {
                    "threshold_db": group.representative_params.threshold_db,
                    "pause_threshold_db": group.representative_params.pause_threshold_db,
                    "peak_db": group.representative_params.peak_db,
                    "mean_db": group.representative_params.mean_db,
                    "sample_rate": group.representative_params.sample_rate,
                }
                cache_dir = preview_cache_root / f"{step_name}_{group.group_id}"
                cache_dir.mkdir(parents=True, exist_ok=True)

                processed_samples = []
                auto_thresholds: list[float] = []
                for sample_path in sample_paths:
                    try:
                        key = Path(sample_path).stem
                        before_path = cache_dir / f"before_{key}.wav"
                        after_path = cache_dir / f"after_{key}.wav"

                        shutil.copy2(sample_path, str(before_path))

                        # silence_trim: two-window transition preview
                        head_envelope: list[float] = []
                        tail_envelope: list[float] = []
                        head_transition_pos: int = 0
                        tail_transition_pos: int = 0
                        segment_url: str = ""
                        tail_segment_url: str = ""
                        if step_name == "silence_trim":
                            try:
                                tp = audio_analysis.compute_threshold_preview(
                                    sample_path,
                                    estimated_threshold_db=params["threshold_db"],
                                )
                                head_envelope = tp["head_envelope"]
                                tail_envelope = tp["tail_envelope"]
                                head_transition_pos = tp["head_transition_pos"]
                                tail_transition_pos = tp["tail_transition_pos"]
                                auto_thresholds.append(tp["auto_threshold_db"])
                                from pydub import AudioSegment as _AS
                                _audio = _AS.from_file(sample_path)
                                # Export head window
                                _h_start = tp.get("window_start_ms", 0)
                                _h_dur = tp.get("window_duration_ms", 500)
                                seg_path = cache_dir / f"seg_{key}.wav"
                                _audio[_h_start:_h_start + _h_dur].export(str(seg_path), format="wav")
                                segment_url = f"{base}?path=ingest_sessions/{task.id}/preview_cache/{step_name}_{group.group_id}/seg_{key}.wav"
                                # Export tail window
                                _t_start = tp.get("tail_window_start_ms", 0)
                                _t_dur = tp.get("tail_window_duration_ms", 500)
                                seg_tail_path = cache_dir / f"seg_tail_{key}.wav"
                                _audio[_t_start:_t_start + _t_dur].export(str(seg_tail_path), format="wav")
                                tail_segment_url = f"{base}?path=ingest_sessions/{task.id}/preview_cache/{step_name}_{group.group_id}/seg_tail_{key}.wav"
                            except Exception:
                                head_envelope = []
                                tail_envelope = []
                                head_transition_pos = 0
                                tail_transition_pos = 0

                        # Process to produce "after"
                        self._run_preview_step(
                            step_name, sample_path, str(after_path),
                            str(cache_dir), params,
                        )

                        # Build relative URL paths for the API
                        base = f"/api/dataset-ingest/{task.id}/audio"
                        processed_samples.append({
                            "key": key,
                            "speaker_id": group.group_id,
                            "before_url": f"{base}?path=ingest_sessions/{task.id}/preview_cache/{step_name}_{group.group_id}/before_{key}.wav",
                            "after_url":  f"{base}?path=ingest_sessions/{task.id}/preview_cache/{step_name}_{group.group_id}/after_{key}.wav",
                            "duration_sec": 0.0,
                            "head_envelope": head_envelope,
                            "tail_envelope": tail_envelope,
                            "head_transition_pos": head_transition_pos,
                            "tail_transition_pos": tail_transition_pos,
                            "segment_url": segment_url,
                            "tail_segment_url": tail_segment_url,
                        })
                    except Exception as e:
                        self._sse_emit(task, db, "error", step=step_name,
                                       message=f"样本处理失败 {Path(sample_path).name}: {e}")

                # For silence_trim: update group threshold with auto-detected value
                if step_name == "silence_trim" and auto_thresholds:
                    avg_thresh = round(float(sum(auto_thresholds) / len(auto_thresholds)), 1)
                    params["threshold_db"] = avg_thresh
                    params["pause_threshold_db"] = avg_thresh

                preview_groups.append({
                    "group_id": group.group_id,
                    "speaker_ids": group.speaker_ids,
                    "representative_params": params,
                    "samples": processed_samples,
                })

            # Store for resume
            input_data[f"{step_name}_preview"] = preview_groups
            input_data["preprocess_pending_step"] = step_name
            input_data["preprocess_step_index"] = i
            self._save_phase_data(task, input_data, db)

            # 4. Pause for user confirmation
            self._sse_emit(task, db, "pause", step=f"preview_{step_name}",
                           payload={"groups": preview_groups})
            self._pause(task, input_data, "post_asr_preprocess", db)
            return  # Wait for user confirmation; on resume, re-enters this method

        # All enabled steps complete — advance to review session
        input_data["phase"] = "review_session"
        self._save_phase_data(task, input_data, db)
        self._phase_review_session(task, input_data, db)

    def _execute_audio_step(self, step_name: str, task, input_data: dict,
                            spk_dirs: list[str], db: Session) -> None:
        """Execute an audio preprocessing step on all speaker directories."""
        output_path = input_data["output_path"]
        preview_data = input_data.get(f"{step_name}_preview", [])

        # Build mapping from speaker_id to group params
        spk_params = {}
        for group_data in preview_data:
            params = group_data.get("representative_params", {})
            for spk_id in group_data.get("speaker_ids", []):
                spk_params[spk_id] = dict(params)

        # Apply user-set thresholds if provided via approve action feedback
        if step_name == "silence_trim":
            try:
                feedback_raw = input_data.get("user_action", {}).get("feedback") or ""
                feedback = json.loads(feedback_raw) if feedback_raw else {}
                group_thresholds = feedback.get("group_thresholds", {})
                for group_data in preview_data:
                    gid = group_data.get("group_id", "")
                    if gid in group_thresholds:
                        user_thresh = float(group_thresholds[gid])
                        for spk_id in group_data.get("speaker_ids", []):
                            if spk_id in spk_params:
                                spk_params[spk_id]["threshold_db"] = user_thresh
                                spk_params[spk_id]["pause_threshold_db"] = user_thresh
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        total = len(spk_dirs)
        completed = 0

        for spk_dir in spk_dirs:
            spk_name = Path(spk_dir).name
            audio_dir = self._audio_dir_of(spk_dir)
            if not audio_dir.is_dir():
                completed += 1
                continue

            audio_exts = {".wav", ".mp3", ".flac"}
            audio_files = sorted(
                p for p in audio_dir.iterdir()
                if p.is_file() and p.suffix.lower() in audio_exts
            )

            if not audio_files:
                completed += 1
                continue

            params = spk_params.get(spk_name, {})
            threshold_db = params.get("threshold_db", -55.0)
            pause_threshold_db = params.get("pause_threshold_db", -55.0)
            float32 = params.get("float32", False)

            for af in audio_files:
                try:
                    if step_name == "silence_trim":
                        # Create temp output path for processed file
                        tmp_out = str(af) + ".trimmed"
                        audio_preprocess.apply_silence_trim(
                            str(af), tmp_out, output_path,
                            threshold_db=threshold_db,
                            float32=float32,
                        )
                        # Replace original with processed
                        os.replace(tmp_out, str(af))
                    elif step_name == "pause_compress":
                        tmp_out = str(af) + ".compressed"
                        audio_preprocess.apply_pause_compress(
                            str(af), tmp_out, output_path,
                            pause_threshold_db=pause_threshold_db,
                            pause_ratio=0.3,
                            float32=float32,
                        )
                        os.replace(tmp_out, str(af))
                    elif step_name == "gain":
                        tmp_out = str(af) + ".gained"
                        audio_preprocess.apply_gain_normalization(
                            str(af), tmp_out, output_path,
                        )
                        os.replace(tmp_out, str(af))
                except Exception as e:
                    self._sse_emit(task, db, "error", step=step_name,
                                   message=f"处理 {af.name} 失败: {e}")

            # segment must run after the per-file loop and update data.jsonl
            if step_name == "segment":
                self._execute_segment_step(spk_dir, audio_files, output_path, task, db)

            completed += 1
            self._sse_emit(task, db, "progress",
                           current=completed, total=total,
                           step=step_name,
                           label=f"{step_name}: {spk_name}")

    def _phase_review_session(self, task, input_data: dict, db: Session) -> None:
        """Merge all data.jsonl into task.input['review_items'].
        _pause() -> awaiting human review via /review-items + /action."""
        output_path = input_data["output_path"]

        spk_dirs = self._collect_speaker_dirs(output_path)
        review_items = []

        for spk_dir in spk_dirs:
            spk_name = Path(spk_dir).name
            jsonl_path = Path(spk_dir) / "data.jsonl"
            if not jsonl_path.is_file():
                continue
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        record["text_edited"] = None
                        record["quality_poor"] = 0
                        record["paralanguage_heavy"] = 0
                        record["hardcode_error"] = 0
                        # Construct playable audio URL — URL-encode path to handle spaces
                        rel_path = record.get("path", "")
                        encoded = quote(f"{spk_name}/{rel_path}", safe="/")
                        record["audio_url"] = (
                            f"/api/dataset-ingest/{task.id}/output-audio"
                            f"?path={encoded}"
                        )
                        review_items.append(record)
                    except json.JSONDecodeError:
                        continue

        input_data["review_items"] = review_items
        self._save_phase_data(task, input_data, db)

        self._sse_emit(task, db, "pause", step="review_session",
                       payload={"total_items": len(review_items)})
        self._pause(task, input_data, "done", db)

    def _phase_done(self, task, input_data: dict, db: Session) -> None:
        """Write back corrected review_items to data.jsonl, update metadata counts,
        final integrity check, generate summary table + duration distribution report."""
        output_path = input_data["output_path"]
        initial_count = input_data["initial_count"]

        # 1. Write back corrected review_items to data.jsonl per speaker
        review_items = input_data.get("review_items", [])
        if review_items:
            # Group by speaker
            speaker_records: dict[str, list[dict]] = {}
            for item in review_items:
                spk = item.get("speaker", "unknown")
                speaker_records.setdefault(spk, []).append(item)

            for spk_name, records in speaker_records.items():
                # Find speaker dir
                spk_dir = Path(output_path) / spk_name
                if not spk_dir.is_dir():
                    # Try pinyin match
                    for entry in Path(output_path).iterdir():
                        if entry.is_dir() and entry.name == spk_name:
                            spk_dir = entry
                            break
                    else:
                        continue

                jsonl_path = spk_dir / "data.jsonl"
                if jsonl_path.is_file():
                    # Read existing to preserve order, update text
                    existing = []
                    with open(jsonl_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                existing.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue

                    # Build lookup from review items
                    review_map = {r["key"]: r for r in records}
                    for rec in existing:
                        key = rec.get("key", "")
                        if key in review_map:
                            ri = review_map[key]
                            # Text correction
                            if ri.get("text_edited"):
                                rec["text"] = ri["text_edited"]
                            # 0/1 quality indicators
                            rec["audio_quality"] = 1 if ri.get("quality_poor") else 0
                            rec["rich_text_level"] = 1 if ri.get("paralanguage_heavy") else 0
                            rec["too_short"] = ri.get("too_short", rec.get("too_short", 0))
                            # hardcode_error stays in flags
                            if ri.get("hardcode_error"):
                                flags = list(rec.get("flags", []))
                                if "hardcode_error" not in flags:
                                    flags.append("hardcode_error")
                                rec["flags"] = flags

                    # Write back
                    with open(jsonl_path, "w", encoding="utf-8") as f:
                        for rec in existing:
                            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        # 2. Update metadata counts (may have changed due to segmentation)
        spk_dirs = self._collect_speaker_dirs(output_path)
        for spk_dir in spk_dirs:
            metadata_path = Path(spk_dir) / "metadata.json"
            audio_dir = self._audio_dir_of(spk_dir)
            if metadata_path.is_file() and audio_dir.is_dir():
                try:
                    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                    meta["count"] = classification.count_audio_files(str(audio_dir))
                    metadata_path.write_text(
                        json.dumps(meta, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

        # 3. Final integrity check
        integrity = self._integrity_check(output_path, initial_count)
        if not integrity.ok:
            self._sse_emit(task, db, "error", step="done",
                           message=f"最终完整性校验失败: {integrity.detail}")

        # 4. Generate summary + duration report
        integrity_model = integrity.model_dump()
        duration_data = self._generate_duration_report(output_path)

        # Count metadata quality, jsonl rows, hardcode errors
        metadata_ok_count = 0
        sample_rate_zero_count = 0
        duration_zero_count = 0
        jsonl_total_rows = 0
        hardcode_error_count = 0
        for spk_dir in spk_dirs:
            metadata_path = Path(spk_dir) / "metadata.json"
            if metadata_path.is_file():
                metadata_ok_count += 1
                try:
                    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if meta.get("sample_rate", 0) == 0:
                        sample_rate_zero_count += 1
                    if meta.get("duration", 0) == 0:
                        duration_zero_count += 1
                except Exception:
                    pass
            jsonl_path = Path(spk_dir) / "data.jsonl"
            if jsonl_path.is_file():
                try:
                    with open(jsonl_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            jsonl_total_rows += 1
                            try:
                                rec = json.loads(line)
                                if "hardcode_error" in rec.get("flags", []):
                                    hardcode_error_count += 1
                            except Exception:
                                pass
                except Exception:
                    pass

        summary = {
            # Fields matching frontend DoneSummary interface exactly
            "initial_count": initial_count,
            "role_count": len(spk_dirs),
            "role_audio_count": integrity_model.get("role_audio_count", 0),
            "special_count": integrity_model.get("special_count", 0),
            "integrity_ok": integrity.ok,
            "metadata_ok_count": metadata_ok_count,
            "sample_rate_zero_count": sample_rate_zero_count,
            "duration_zero_count": duration_zero_count,
            "jsonl_total_rows": jsonl_total_rows,
            "hardcode_error_count": hardcode_error_count,
            "output_path": output_path,
            "duration_distribution": duration_data["distribution"],
            "top_roles": duration_data["top_roles"],
            "total_duration_h": duration_data["total_duration_h"],
        }

        # 5. Store in task.result
        task.result = json.dumps(summary, ensure_ascii=False)

        # 6. Emit done
        self._sse_emit(task, db, "done", summary=summary)

        # 7. Clean up preview_cache — temporary audio samples no longer needed
        preview_cache = STORAGE_DIR / "ingest_sessions" / task.id / "preview_cache"
        if preview_cache.exists():
            shutil.rmtree(str(preview_cache), ignore_errors=True)

        self._save_phase_data(task, input_data, db)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_post_execution_review(self, step_name: str, task, input_data: dict, db) -> dict | None:
        """Generic post-execution before/after review for any audio modification step.

        Uses {step_name}_preview data for before samples,
        finds processed files in output_path for after samples.
        Works for silence_trim, pause_compress, gain, etc.
        """
        output_path = input_data["output_path"]
        preview_data = input_data.get(f"{step_name}_preview", [])
        if not preview_data:
            return None

        preview_cache_root = STORAGE_DIR / "ingest_sessions" / task.id / "preview_cache"
        base = f"/api/dataset-ingest/{task.id}/audio"
        audio_exts = {".wav", ".mp3", ".flac"}

        audio_lookup: dict[str, str] = {}
        for spk_dir in self._collect_speaker_dirs(output_path):
            audio_dir = self._audio_dir_of(spk_dir)
            if audio_dir.is_dir():
                for af in audio_dir.iterdir():
                    if af.is_file() and af.suffix.lower() in audio_exts:
                        audio_lookup[af.stem] = str(af)

        review_groups = []
        for group_data in preview_data:
            gid = group_data.get("group_id", "")
            review_dir = preview_cache_root / f"{step_name}_review_{gid}"
            review_dir.mkdir(parents=True, exist_ok=True)

            review_samples = []
            for s in group_data.get("samples", []):
                key = s.get("key", "")
                before_url = s.get("before_url", "")
                if not before_url:
                    continue
                processed_path = audio_lookup.get(key)
                if not processed_path:
                    continue
                try:
                    dst = review_dir / f"after_exec_{key}.wav"
                    shutil.copy2(processed_path, str(dst))
                    after_url = (
                        f"{base}?path=ingest_sessions/{task.id}/preview_cache"
                        f"/{step_name}_review_{gid}/after_exec_{key}.wav"
                    )
                    review_samples.append({
                        "key": key,
                        "speaker_id": gid,
                        "before_url": before_url,
                        "after_url": after_url,
                        "duration_sec": 0.0,
                    })
                except Exception as e:
                    self._sse_emit(task, db, "error", step=f"{step_name}_review",
                                   message=f"生成对比样本失败 {key}: {e}")

            if review_samples:
                review_groups.append({
                    "group_id": gid,
                    "speaker_ids": group_data.get("speaker_ids", []),
                    "representative_params": group_data.get("representative_params", {}),
                    "samples": review_samples,
                })

        return {"groups": review_groups} if review_groups else None

    def _build_silence_trim_review(self, task, input_data: dict, db) -> dict | None:
        """Wrapper: silence_trim post-execution review."""
        return self._build_post_execution_review("silence_trim", task, input_data, db)

    def _execute_segment_step(self, spk_dir: str, audio_files: list,
                              output_path: str, task, db,
                              silence_threshold_sec: float = 2.0) -> dict:
        """Split audio files at silence gaps, updating data.jsonl with new entries.

        Returns {"files_scanned": int, "files_split": int, "segments_created": int}
        """
        jsonl_path = Path(spk_dir) / "data.jsonl"
        if not jsonl_path.is_file():
            return {"files_scanned": 0, "files_split": 0, "segments_created": 0}

        existing = []
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        existing.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        audio_dir = self._audio_dir_of(spk_dir)
        key_to_rec = {r["key"]: r for r in existing}
        audio_keys = {af.stem for af in audio_files}

        new_records = []
        any_split = False
        files_split = 0
        segments_created = 0

        for af in audio_files:
            stem = af.stem
            rec = key_to_rec.get(stem, {"key": stem, "path": f"audio/{af.name}",
                                        "text": "", "rich_text": "", "emotion": "",
                                        "speaker": Path(spk_dir).name, "duration": 0.0,
                                        "audio_quality": 0, "rich_text_level": 0,
                                        "too_short": 0, "flags": []})
            asr_text = rec.get("text", "")

            tmp_dir = audio_dir / f"_segtmp_{stem}"
            tmp_dir.mkdir(exist_ok=True)
            try:
                segs = audio_preprocess.apply_sentence_segmentation(
                    str(af), asr_text,
                    str(tmp_dir), output_path,
                    silence_threshold_sec=silence_threshold_sec,
                )

                if len(segs) <= 1:
                    for s in segs:
                        p = Path(s["path"])
                        if p.exists():
                            p.unlink()
                    new_records.append(rec)
                    continue

                any_split = True
                files_split += 1
                segments_created += len(segs)
                for i, seg in enumerate(segs):
                    seg_stem = f"{stem}_s{i:03d}"
                    dst = audio_dir / f"{seg_stem}{af.suffix}"
                    Path(seg["path"]).rename(dst)
                    seg_rec = {
                        "key": seg_stem,
                        "path": f"audio/{dst.name}",
                        "text": seg["text"],
                        "rich_text": rec.get("rich_text", ""),
                        "emotion": rec.get("emotion", ""),
                        "speaker": rec.get("speaker", Path(spk_dir).name),
                        "duration": round(seg["end_sec"] - seg["start_sec"], 6),
                        "audio_quality": rec.get("audio_quality", 0),
                        "rich_text_level": rec.get("rich_text_level", 0),
                        "too_short": rec.get("too_short", 0),
                        "flags": list(rec.get("flags", [])),
                    }
                    new_records.append(seg_rec)
                af.unlink()

            except Exception as e:
                self._sse_emit(task, db, "error", step="segment",
                               message=f"切句 {af.name} 失败: {e}")
                new_records.append(rec)
            finally:
                shutil.rmtree(str(tmp_dir), ignore_errors=True)

        for rec in existing:
            if rec.get("key") not in audio_keys:
                new_records.append(rec)

        if any_split:
            with open(jsonl_path, "w", encoding="utf-8") as f:
                for rec in new_records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        return {
            "files_scanned": len(audio_files),
            "files_split": files_split,
            "segments_created": segments_created,
        }

    def _reset_output_from_source(self, output_path: str, input_data: dict,
                                   task, db) -> None:
        """Delete output_path entirely and re-copy from source.

        Called when the user rejects classification — simpler and more reliable
        than reversing individual file moves.  The source_path / upload_path
        remain in input_data from the API so we can re-use them here.
        """
        # 1. Wipe output directory
        shutil.rmtree(output_path, ignore_errors=True)
        os.makedirs(output_path, exist_ok=True)

        # 2. Re-populate from source
        source_type = input_data.get("source_type", "server_path")
        if source_type == "server_path":
            source_path = input_data.get("source_path", "")
            if source_path and os.path.exists(source_path):
                shutil.copytree(source_path, output_path, dirs_exist_ok=True)
            else:
                self._sse_emit(task, db, "error", step="classify_review",
                               message=f"source_path 不可访问: {source_path}")
                return
        elif source_type == "upload":
            upload_path = input_data.get("upload_path", "")
            if upload_path and os.path.exists(upload_path):
                import zipfile
                with zipfile.ZipFile(upload_path) as zf:
                    zf.extractall(output_path)
            else:
                self._sse_emit(task, db, "error", step="classify_review",
                               message=f"上传文件不可访问: {upload_path}")
                return

        # 3. Re-scan and update initial_count + original_paths
        struct = classification.scan_directory(output_path)
        input_data["original_paths"] = struct.original_paths
        input_data["initial_count"] = classification.count_audio_files(output_path)
        input_data["dataset_structure"] = struct.structure_type

        # 4. Rebuild speaker_map from fresh copy
        speaker_map = self._build_speaker_map(output_path,
                                              structure_type=struct.structure_type)
        input_data["speaker_map"] = speaker_map
        map_path = Path(output_path) / "_speaker_map.json"
        map_path.write_text(json.dumps(speaker_map, ensure_ascii=False, indent=2),
                            encoding="utf-8")

        self._save_phase_data(task, input_data, db)
        self._sse_emit(task, db, "progress", step="classify_review",
                       label=f"输出目录已重置，{input_data['initial_count']} 个音频文件就绪")

    def _build_speaker_map(self, output_path: str, structure_type: str = "flat") -> dict:
        """Build a preliminary speaker map from current directory structure.

        For cv_hierarchy: top-level dirs are CV/actor names; speakers are one level deeper.
        For flat: top-level dirs are speaker dirs directly.

        Returns:
        {
            "original_folder_name": {
                "speaker_name": "folder_name",
                "actor": "cv_name or ''",
                "files": ["file1.wav", ...],
                "count": 42
            }
        }
        """
        from backend.tools.classification import count_audio_files, AUDIO_EXTENSIONS

        p = Path(output_path)
        mapping: dict = {}

        if structure_type == "cv_hierarchy":
            # Level 1 = CV/actor dirs; level 2 = speaker dirs
            for cv_entry in sorted(p.iterdir()):
                if not cv_entry.is_dir() or cv_entry.name.startswith(".") or cv_entry.name == "_special":
                    continue
                actor_name = cv_entry.name
                for spk_entry in sorted(cv_entry.iterdir()):
                    if not spk_entry.is_dir() or spk_entry.name.startswith("."):
                        continue
                    sample_files: list[str] = []
                    for f in spk_entry.rglob("*"):
                        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                            sample_files.append(f.name)
                            if len(sample_files) >= 3:
                                break
                    mapping[spk_entry.name] = {
                        "speaker_name": spk_entry.name,
                        "actor": actor_name,
                        "files": sample_files,
                        "count": count_audio_files(str(spk_entry)),
                    }
        else:
            # Flat: top-level dirs are speakers
            for entry in sorted(p.iterdir()):
                if not entry.is_dir() or entry.name.startswith(".") or entry.name == "_special":
                    continue
                sample_files = []
                for f in entry.rglob("*"):
                    if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                        sample_files.append(f.name)
                        if len(sample_files) >= 3:
                            break
                mapping[entry.name] = {
                    "speaker_name": entry.name,
                    "actor": "",
                    "files": sample_files,
                    "count": count_audio_files(str(entry)),
                }
        return mapping

    def _sse_emit(self, task, db: Session, event_type: str, **kwargs) -> None:
        """Append SSE event to task.logs for the SSE endpoint to pick up."""
        import time as _time
        payload = {"type": event_type, "ts": int(_time.time() * 1000), **kwargs}
        task.append_log(json.dumps(payload, ensure_ascii=False))
        db.commit()

    def _integrity_check(self, output_path: str, initial_count: int) -> IntegrityResult:
        """Assert role/audio files + _special/ files == initial_count."""
        role_audio_count = 0
        for spk_dir in self._collect_speaker_dirs(output_path):
            audio_dir = self._audio_dir_of(spk_dir)
            role_audio_count += classification.count_audio_files(str(audio_dir))

        special_path = Path(output_path) / '_special'
        special_count = classification.count_audio_files(str(special_path))

        current_total = role_audio_count + special_count
        return IntegrityResult(
            ok=(current_total == initial_count),
            initial_count=initial_count,
            role_audio_count=role_audio_count,
            special_count=special_count,
            current_total=current_total,
            detail=(
                f"role={role_audio_count} special={special_count} "
                f"total={current_total} expected={initial_count}"
            ),
        )

    def _parse_classify_response(self, response: str) -> tuple[str, list[dict]]:
        """Parse LLM response into (script, merge_candidates).

        Handles both raw Python and markdown-fenced responses (```python...```).
        """
        merge_candidates: list[dict] = []

        # Extract MERGE_CANDIDATES before stripping fences
        merge_pattern = r'#\s*MERGE_CANDIDATES\s*:\s*(\[.*?\])'
        merge_match = re.search(merge_pattern, response, re.DOTALL)
        if merge_match:
            try:
                merge_candidates = json.loads(merge_match.group(1))
            except json.JSONDecodeError:
                pass

        # Strip markdown code fences (```python ... ``` or ``` ... ```)
        # LLMs like DeepSeek often wrap code in fenced blocks
        fenced = re.search(r'```(?:python)?\s*\n(.*?)```', response, re.DOTALL)
        if fenced:
            script = fenced.group(1).strip()
        else:
            script = response.strip()

        # Remove MERGE_CANDIDATES comment from the final script
        script = re.sub(merge_pattern, "", script, flags=re.DOTALL).strip()

        return script, merge_candidates

    def _fix_script_with_llm(self, script: str, error: str, input_data: dict, task, db) -> str | None:
        """Ask LLM to fix a script that failed sandbox validation.

        Returns the fixed script string, or None if the LLM call fails entirely.
        """
        fix_prompt = f"""以下 Python 分类脚本在安全沙箱检查中失败，请修复。

=== 沙箱错误信息 ===
{error}

=== 沙箱检查规则说明 ===
沙箱共 6 层检查，常见失败原因：
- Layer 1 (AST 静态分析): 禁止 import subprocess/socket/sys/os.system 等危险模块
- Layer 2 (OpLevel): DELETE 操作需要路径在 output_path 内；禁止 eval/exec
- Layer 3 (路径验证): 脚本中所有目标路径字面量必须以 output_path 开头。
  ⚠️ 常见错误：路径字面量中含有 .wav/.mp3/.flac 扩展名（沙箱认为是文件路径直接操作，要求必须在 output_path 内）
  修复：不要在路径字面量里写文件扩展名，对扩展名的判断用变量/条件判断，不要硬编码带扩展名的路径
- Layer 4 (运行时 _SafeShutil): shutil.move/rmtree 的目标路径在运行时必须在 output_path 内
- Layer 5 (干运行): 脚本逻辑必须能在 dry_run=True 模式下正确执行

=== 变量说明（由执行器注入，可直接使用）===
- output_path: str  目标输出目录（所有文件必须移动到这里）
- original_paths: list[str]  原始目录列表（cv_hierarchy 则每个元素是 CV 目录）
- initial_count: int  原始音频文件总数

=== 原始脚本 ===
```python
{script}
```

=== 修复要求 ===
1. 所有 shutil.move 目标路径必须在 output_path 内
2. 禁止直接使用文件扩展名作为路径字面量（如 "/path/file.wav"），
   用 f-string 或 os.path.join 拼接：dst = os.path.join(output_path, role, 'audio', f.name)
3. 禁止导入 subprocess, socket, requests, sys 等模块
4. 必须遍历所有 original_paths，不能只处理 original_paths[0]
5. 每个角色目录必须在 output_path 下创建 audio/ 子目录

只返回修复后的完整 Python 代码，不加任何解释或 markdown 包裹。"""

        full_response = ""
        try:
            import anthropic

            client_kwargs: dict = {}
            if ANTHROPIC_API_KEY:
                client_kwargs["api_key"] = ANTHROPIC_API_KEY
            elif ANTHROPIC_AUTH_TOKEN:
                client_kwargs["auth_token"] = ANTHROPIC_AUTH_TOKEN
            if ANTHROPIC_BASE_URL:
                client_kwargs["base_url"] = ANTHROPIC_BASE_URL

            client = anthropic.Anthropic(**client_kwargs, timeout=120.0)
            with client.messages.stream(
                model=ANTHROPIC_MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": fix_prompt}],
            ) as stream:
                thinking_buf = ""
                for text in stream.text_stream:
                    full_response += text
                    thinking_buf += text
                    if len(thinking_buf) >= 10:
                        self._sse_emit(task, db, "thinking", content=thinking_buf)
                        thinking_buf = ""
                if thinking_buf:
                    self._sse_emit(task, db, "thinking", content=thinking_buf)
        except Exception as e:
            self._sse_emit(task, db, "error", step="classify_review",
                           message=f"LLM 修复脚本失败: {e}")
            return None

        fixed_script, _ = self._parse_classify_response(full_response)
        if not fixed_script.strip():
            return None
        return fixed_script

    def _build_experience_entry(self, input_data: dict) -> str:
        """Build a classification_experience.md entry from input_data."""
        project_code = input_data.get("project_code", "unknown")
        date = datetime.utcnow().strftime("%Y-%m-%d")
        structure_type = input_data.get("dataset_structure", "unknown")
        merge_candidates = input_data.get("merge_candidates", [])

        entry = f"""## {project_code} ({date})

**结构类型**: {structure_type}
**合并规则**: {json.dumps(merge_candidates, ensure_ascii=False) if merge_candidates else '无'}
**Gotchas**: 无
"""
        return entry

    @staticmethod
    def _audio_dir_of(spk_dir: str) -> Path:
        """Return the directory containing audio files for a speaker.
        Prefers {spk_dir}/audio/ if it exists, else falls back to {spk_dir}/ itself."""
        p = Path(spk_dir)
        audio = p / 'audio'
        return audio if audio.is_dir() else p

    def _normalize_output_structure(self, output_path: str) -> int:
        """Harness-level guarantee: ensure every role dir has an audio/ subdir.

        Handles three patterns left by LLM classification scripts:
          1. role/audio/*.wav  → correct, skip
          2. role/*.wav        → create role/audio/ and move files in
          3. cv/role/*.wav     → flatten: lift role dirs to output_path level,
                                 then apply pattern 2

        Never touches _special/.  Returns number of audio files moved.
        """
        p = Path(output_path)
        audio_exts = {'.wav', '.mp3', '.flac'}

        def _has_direct_audio(d: Path) -> bool:
            return any(f.suffix.lower() in audio_exts for f in d.iterdir() if f.is_file())

        def _ensure_audio_subdir(role_dir: Path) -> int:
            audio_files = [
                f for f in role_dir.iterdir()
                if f.is_file() and f.suffix.lower() in audio_exts
            ]
            if not audio_files:
                return 0
            audio_sub = role_dir / 'audio'
            audio_sub.mkdir(exist_ok=True)
            for f in audio_files:
                shutil.move(str(f), str(audio_sub / f.name))
            return len(audio_files)

        moved = 0
        for entry in sorted(p.iterdir()):
            if not entry.is_dir() or entry.name.startswith('.') or entry.name == '_special':
                continue

            if (entry / 'audio').is_dir():
                continue  # already correct

            if _has_direct_audio(entry):
                # Pattern 2: flat without audio/ subdir
                moved += _ensure_audio_subdir(entry)
            else:
                # Pattern 3: possible cv_hierarchy — check one level deeper
                subs_with_audio = [
                    s for s in entry.iterdir()
                    if s.is_dir() and not s.name.startswith('.') and _has_direct_audio(s)
                ]
                if not subs_with_audio:
                    continue
                for sub in subs_with_audio:
                    dst = p / sub.name
                    if dst.exists() and dst.is_dir():
                        # Merge into existing destination
                        dst_audio = dst / 'audio'
                        dst_audio.mkdir(exist_ok=True)
                        for f in [f for f in sub.iterdir()
                                  if f.is_file() and f.suffix.lower() in audio_exts]:
                            shutil.move(str(f), str(dst_audio / f.name))
                            moved += 1
                    else:
                        shutil.move(str(sub), str(dst))
                        moved += _ensure_audio_subdir(dst)
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
                try:
                    entry.rmdir()  # remove now-empty CV dir
                except OSError:
                    pass

        return moved

    def _collect_speaker_dirs(self, output_path: str) -> list[str]:
        """Return sorted speaker dirs. Handles flat (role/audio/) and cv_hierarchy (cv/role/) outputs."""
        p = Path(output_path)
        if not p.is_dir():
            return []
        audio_exts = {'.wav', '.mp3', '.flac'}

        def _has_audio(d: Path) -> bool:
            try:
                return any(f.suffix.lower() in audio_exts for f in d.iterdir() if f.is_file())
            except PermissionError:
                return False

        result = []
        for entry in sorted(p.iterdir()):
            if not entry.is_dir() or entry.name.startswith('.') or entry.name == '_special':
                continue
            if (entry / 'audio').is_dir() or _has_audio(entry):
                result.append(str(entry))
            else:
                # cv_hierarchy: check one level deeper
                for sub in sorted(entry.iterdir()):
                    if not sub.is_dir() or sub.name.startswith('.'):
                        continue
                    if (sub / 'audio').is_dir() or _has_audio(sub):
                        result.append(str(sub))
        return result

    def _ffprobe_audio(self, audio_path: str) -> tuple[int, str]:
        """Run ffprobe on audio_path, return (sample_rate, format).
        MUST filter codec_type == 'audio' or sample_rate will be 0."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "a", audio_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return (0, "unknown")

            info = json.loads(result.stdout)
            streams = info.get("streams", [])

            sample_rate = 0
            codec_name = "unknown"
            for stream in streams:
                if stream.get("codec_type") == "audio":
                    sr = stream.get("sample_rate")
                    if sr is not None:
                        sample_rate = int(sr)
                    codec = stream.get("codec_name", "unknown")
                    if codec:
                        codec_name = codec
                    break

            # Map codec to format
            fmt = "wav"
            if codec_name in ("mp3", "mp3float"):
                fmt = "mp3"
            elif codec_name in ("flac",):
                fmt = "flac"

            return (sample_rate, fmt)

        except Exception:
            return (0, "unknown")

    def _ffprobe_duration(self, audio_path: str) -> float:
        """Get duration of a single audio file via ffprobe. Returns seconds as float."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "a", audio_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return 0.0

            info = json.loads(result.stdout)
            streams = info.get("streams", [])

            for stream in streams:
                if stream.get("codec_type") == "audio":
                    dur = stream.get("duration")
                    if dur is not None:
                        return float(dur)

            # Fallback: try format-level duration
            fmt_info = info.get("format", {})
            dur = fmt_info.get("duration")
            if dur is not None:
                return float(dur)

            return 0.0

        except Exception:
            return 0.0

    def _execute_flag_short(self, task, input_data: dict,
                            spk_dirs: list[str], db: Session) -> dict:
        """Flag records where len(text) < 3. Returns {total_flagged, samples}."""
        total_flagged = 0
        samples: list[dict] = []
        for spk_dir in spk_dirs:
            jsonl_path = Path(spk_dir) / "data.jsonl"
            if not jsonl_path.is_file():
                continue
            records = []
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if len(rec.get("text", "")) < 3:
                        rec["too_short"] = 1
                        total_flagged += 1
                        if len(samples) < 10:
                            samples.append({"key": rec.get("key", ""), "text": rec.get("text", ""),
                                            "speaker": rec.get("speaker", ""), "duration": rec.get("duration", 0.0)})
                    records.append(rec)
            with open(jsonl_path, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        return {"total_flagged": total_flagged, "samples": samples}

    def _execute_flag_richtext(self, task, input_data: dict,
                               spk_dirs: list[str], db: Session) -> dict:
        """Flag records with high rich-text ratio. Returns {total_flagged, samples}."""
        total_flagged = 0
        samples: list[dict] = []
        for spk_dir in spk_dirs:
            jsonl_path = Path(spk_dir) / "data.jsonl"
            if not jsonl_path.is_file():
                continue
            records = []
            texts = []
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    records.append(rec)
                    texts.append(rec.get("text", ""))

            if not texts:
                continue

            results = rich_text_detector.flag_batch(texts)
            for i, result in enumerate(results):
                if result.get("flagged"):
                    flags = records[i].get("flags", [])
                    if "high_rich_text" not in flags:
                        flags.append("high_rich_text")
                        records[i]["flags"] = flags
                    records[i]["rich_text_level"] = 1
                    total_flagged += 1
                    if len(samples) < 10:
                        samples.append({"key": records[i].get("key", ""), "text": records[i].get("text", ""),
                                        "speaker": records[i].get("speaker", "")})

            with open(jsonl_path, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        return {"total_flagged": total_flagged, "samples": samples}

    def _generate_duration_report(self, output_path: str) -> str:
        """Build bucket distribution + top-10 table from metadata.json files
        (no ffprobe re-run)."""
        p = Path(output_path)
        durations: list[tuple[str, int, float]] = []  # (name, count, duration_hours)

        for entry in sorted(p.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name == "_special":
                continue
            metadata_path = entry / "metadata.json"
            if metadata_path.is_file():
                try:
                    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                    name = meta.get("speaker_name", entry.name)
                    count = meta.get("count", 0)
                    dur_h = meta.get("duration", 0)
                    durations.append((name, count, dur_h))
                except Exception:
                    pass

        # Buckets (in hours): 0-10s=0.0028h, 10-30s=0.0083h, 30s-1m=0.0167h,
        # 1-3m=0.05h, 3-10m=0.167h, 10-30m=0.5h, 30m-1h=1h, 1h+
        buckets = [
            ("0-10s", 0, 10 / 3600.0),
            ("10-30s", 10 / 3600.0, 30 / 3600.0),
            ("30s-1m", 30 / 3600.0, 60 / 3600.0),
            ("1-3m", 1 / 60.0, 3 / 60.0),
            ("3-10m", 3 / 60.0, 10 / 60.0),
            ("10-30m", 10 / 60.0, 30 / 60.0),
            ("30m-1h", 0.5, 1.0),
            ("1h+", 1.0, float("inf")),
        ]

        bucket_counts = [0] * len(buckets)
        for _, _, dur_h in durations:
            for i, (_, lo, hi) in enumerate(buckets):
                if lo <= dur_h < hi:
                    bucket_counts[i] += 1
                    break

        # Top 10 by duration
        sorted_durs = sorted(durations, key=lambda x: x[2], reverse=True)[:10]
        total_h = sum(d[2] for d in durations)

        # Return structured data matching frontend DoneSummary interface
        return {
            "distribution": {label: count for (label, _, _), count in zip(buckets, bucket_counts)},
            "top_roles": [
                {"name": name, "count": count, "duration_h": round(dur_h, 3)}
                for name, count, dur_h in sorted_durs
            ],
            "total_duration_h": round(total_h, 2),
        }

    def _run_preview_step(
        self,
        step_name: str,
        src_path: str,
        dst_path: str,
        output_root: str,
        params: dict,
    ) -> None:
        """Apply one audio processing step to a single sample file for preview."""
        if step_name == "silence_trim":
            audio_preprocess.apply_silence_trim(
                audio_path=src_path,
                output_path=dst_path,
                output_root=output_root,
                threshold_db=params.get("threshold_db", -55.0),
                hold_ms=50,
                head_tail_ms=100,
            )
        elif step_name == "pause_compress":
            audio_preprocess.apply_pause_compress(
                audio_path=src_path,
                output_path=dst_path,
                output_root=output_root,
                pause_threshold_db=params.get("pause_threshold_db", -55.0),
                offset_hold_ms=50,
                pause_threshold=400,
                pause_ratio=0.3,
                pause_cap=1500,
            )
        elif step_name == "gain":
            audio_preprocess.apply_gain_normalization(
                audio_path=src_path,
                output_path=dst_path,
                output_root=output_root,
                peak_target=0.7,
            )
        elif step_name == "segment":
            # For segment preview just copy — cuts happen at full-batch stage
            shutil.copy2(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path)

    def _append_experience(self, input_data: dict) -> None:
        """After user approves classification, append confirmed rules
        to classification_experience.md."""
        experience_entry = self._build_experience_entry(input_data)
        classification.append_experience_entry(experience_entry)
