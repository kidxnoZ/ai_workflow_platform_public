"""PromptExperimentExecutor：5 阶段克隆实验室执行器（v2）。"""
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from pydub import AudioSegment
from sqlalchemy.orm import Session

import backend.config as config
from backend.executors.agent_base import AgentBaseExecutor, AgentExecutorRegistry
from backend.models.task import Task, ModelRegistry, PromptExperiment
from backend.tools.audio import segment_by_silence
from backend.tools.wav_utils import concat_wavs

from backend.config import OUTPUTS_DIR as OUTPUTS_BASE

# ── 5D 约束规则表 ─────────────────────────────────────────────────────────────

_CONTENT_RULES: dict[str, str] = {
    "技能台词": "短促有力（8-15字），含动作动词，语气铿锵，句末不加句号",
    "游戏对话": "口语化自然对话感（10-30字），可用「啊」「哦」「诶」等语气词",
    "过场旁白": "叙述流畅情感饱满（20-50字），允许长句",
    "广告宣传": "节奏感强朗朗上口（10-25字），可押韵",
    "有声书故事": "叙事性描述（25-60字），文学感强",
}

_EMOTION_RULES: dict[str, str] = {
    "中性": "语气平和，避免强烈情感词汇，少用感叹号",
    "活泼开朗": "多用语气词（啊/哦/诶），使用感叹号，轻松愉快",
    "沉稳内敛": "语气沉稳，句式工整，禁止感叹号和语气词，措辞正式",
    "情感丰富": "情感词汇丰富，语气有起伏，喜怒哀乐均可",
    "低沉压抑": "措辞沉重，少标点，暗色调词汇",
    "夸张戏剧": "夸张表达，反问句感叹句交替，戏剧化转折",
}

_LANGUAGE_RULES: dict[str, str] = {
    "口语日常": "使用日常口语，避免书面语和文言词",
    "文言古风": "使用文言词汇（汝/吾/之/乃等），句式简短凝练",
    "赛博朋克": "含未来科技词汇，英文缩写可出现，语感硬朗",
    "轻小说风": "现代轻小说语感，二次元用词，强对白感",
}


def _prompt_hash(combo_id: str) -> str:
    return hashlib.md5(combo_id.encode()).hexdigest()[:12]


def _build_client():
    api_key = config.ANTHROPIC_AUTH_TOKEN or config.ANTHROPIC_API_KEY
    if not api_key:
        return None
    import anthropic
    kwargs: dict = {"api_key": api_key}
    if config.ANTHROPIC_BASE_URL:
        kwargs["base_url"] = config.ANTHROPIC_BASE_URL
    return anthropic.Anthropic(**kwargs)


def _call_claude(client, prompt: str, max_tokens: int = 2048) -> str:
    """用流式请求避免网关长连接超时。"""
    text_parts: list[str] = []
    with client.messages.stream(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            text_parts.append(text)
    return "".join(text_parts)


class PromptExperimentExecutor(AgentBaseExecutor):

    # ──────────────────────────────────────────────────────────────────────
    # Phase 1：Whisper ASR（只转写，暂停等用户校对）
    # ──────────────────────────────────────────────────────────────────────
    def _phase_start(self, task, input_data: dict, db: Session) -> None:
        task.append_log("Phase start: Whisper ASR 转写")
        db.commit()

        prompts = input_data.get("prompts", [])

        try:
            import whisper as _whisper
            wmodel = _whisper.load_model(getattr(config, "WHISPER_MODEL", "base"))
            task.append_log("Whisper 模型加载完成")
            db.commit()
            for p in prompts:
                if not p.get("asr_text"):
                    stored_path = p.get("stored_path", "")
                    if stored_path and os.path.isfile(stored_path):
                        result = wmodel.transcribe(stored_path, language="zh", initial_prompt="以下是普通话语音的简体中文转写。")
                        p["asr_text"] = result.get("text", "").strip()
                        task.append_log(
                            f"ASR: {os.path.basename(stored_path)} → {p['asr_text'][:60]}"
                        )
        except Exception as e:
            task.append_log(f"Whisper ASR 失败（{e}），请手动填写", "warn")

        input_data["prompts"] = prompts
        self._pause(task, input_data, "confirm_asr", db)

    # ──────────────────────────────────────────────────────────────────────
    # Phase 2：5D 约束文本生成（ASR 已由用户校对）
    # ──────────────────────────────────────────────────────────────────────
    def _phase_confirm_asr(self, task, input_data: dict, db: Session) -> None:
        task.append_log("Phase confirm_asr: AI 生成测试文本")
        db.commit()

        prompts = input_data.get("prompts", [])
        business_context = input_data.get("business_context", {})
        test_text_count = input_data.get("test_text_count", 10)
        synthesis_scene = input_data.get("synthesis_scene", "")

        asr_texts = [p.get("asr_text", "") for p in prompts if p.get("asr_text")]

        ctx = business_context
        tag_constraints: list[str] = []
        emotion_rule = _EMOTION_RULES.get(ctx.get("emotion_register", ""), "")
        content_rule = _CONTENT_RULES.get(ctx.get("content_type", ""), "")
        language_rule = _LANGUAGE_RULES.get(ctx.get("language_style", ""), "")
        if emotion_rule:
            tag_constraints.append(f"情感基调「{ctx.get('emotion_register', '')}」：{emotion_rule}")
        if content_rule:
            tag_constraints.append(f"内容类型「{ctx.get('content_type', '')}」：{content_rule}")
        if language_rule:
            tag_constraints.append(f"语言风格「{ctx.get('language_style', '')}」：{language_rule}")
        if ctx.get("special_req") and ctx["special_req"] not in ("无", ""):
            tag_constraints.append(f"特殊要求：{ctx['special_req']}")

        generated_texts: list[str] = []
        client = _build_client()
        if not client:
            task.append_log("ANTHROPIC_AUTH_TOKEN 未配置，跳过 AI 生成")
        elif not asr_texts:
            task.append_log("无 ASR 文本，跳过 AI 生成", "warn")
        else:
            try:
                asr_sample = "\n".join(f"- {t}" for t in asr_texts[:5])
                constraint_block = (
                    "\n".join(f"  • {r}" for r in tag_constraints)
                    if tag_constraints else "  （无特定约束）"
                )
                if synthesis_scene:
                    scene_instruction = (
                        f"\n【合成目标场景】{synthesis_scene}\n"
                        f"生成的文本应符合此目标场景的语义和主题，同时严格遵守上述5D约束。\n"
                        f"参考音频的ASR文本仅供了解音色风格，不代表目标内容方向。"
                    )
                else:
                    scene_instruction = (
                        "\n【合成方式】模仿参考音频的内容风格（主题、句式、氛围），生成类似场景下的新文本。"
                    )
                user_prompt = (
                    f"你是 TTS 合成文本生成专家。\n\n"
                    f"【5D 场景标签】\n"
                    f"  角色类型：{ctx.get('character_type', '')}\n"
                    f"  情感基调：{ctx.get('emotion_register', '')}\n"
                    f"  内容类型：{ctx.get('content_type', '')}\n"
                    f"  语言风格：{ctx.get('language_style', '')}\n"
                    f"  特殊要求：{ctx.get('special_req', '无')}\n\n"
                    f"【生成约束（必须严格遵守）】\n{constraint_block}\n"
                    f"{scene_instruction}\n\n"
                    f"【参考音频 ASR 文本】\n{asr_sample}\n\n"
                    f"请生成 {test_text_count} 条中文测试句，要求：\n"
                    f"1. 严格遵守所有生成约束\n"
                    f"2. 文本间风格多样，避免重复\n"
                    f"3. 仅返回 JSON 数组，例如：[\"句子1\", \"句子2\"]，不要其他内容"
                )
                raw = _call_claude(client, user_prompt, max_tokens=1024)
                s, e = raw.find("["), raw.rfind("]")
                if s != -1 and e != -1:
                    generated_texts = json.loads(raw[s:e + 1])
                task.append_log(
                    f"Claude 生成 {len(generated_texts)} 条文本"
                    f"（场景={'有' if synthesis_scene else '无'}）"
                )
            except Exception as exc:
                task.append_log(f"Claude 生成失败（{exc}）", "warn")

        input_data["generated_texts"] = generated_texts
        db.commit()
        self._pause(task, input_data, "confirm_texts", db)

    # ──────────────────────────────────────────────────────────────────────
    # Phase 2：切分音频 + 知识库查询 + AI 推理 combo 规划
    # ──────────────────────────────────────────────────────────────────────
    def _phase_confirm_texts(self, task, input_data: dict, db: Session) -> None:
        confirmed_texts = input_data.get("confirmed_texts", [])
        if not confirmed_texts:
            raise ValueError("confirmed_texts 不能为空")

        task.append_log(
            f"Phase confirm_texts: {len(confirmed_texts)} 条文本，查询知识库 + AI 推理规划"
        )
        db.commit()

        prompts = input_data.get("prompts", [])
        model_ids = input_data.get("model_ids", [])
        params_map = input_data.get("params_map", {})
        context_key = input_data.get("context_key", "")
        business_context = input_data.get("business_context", {})

        # Step 1: 切分 prompt 音频
        split_prompts: dict[str, str] = {}
        prompt_asr_map: dict[str, str] = {}
        for idx, p in enumerate(prompts):
            stored_path = p.get("stored_path", "")
            asr_text = p.get("asr_text", "")
            if not stored_path or not os.path.isfile(stored_path):
                task.append_log(f"prompt {idx} 文件不存在: {stored_path}", "warn")
                split_prompts[f"p{idx}"] = stored_path
                prompt_asr_map[f"p{idx}"] = asr_text
                continue
            audio = AudioSegment.from_file(stored_path)
            segments = segment_by_silence(audio, min_silence_ms=3000)
            if len(segments) > 1:
                for i, (s, e) in enumerate(segments):
                    seg_path = f"/tmp/{task.id}_p{idx}_{i}.wav"
                    audio[s:e].export(seg_path, format="wav")
                    split_prompts[f"p{idx}.{i}"] = seg_path
                    prompt_asr_map[f"p{idx}.{i}"] = asr_text
                task.append_log(f"prompt {idx} 切分为 {len(segments)} 段")
            else:
                split_prompts[f"p{idx}"] = stored_path
                prompt_asr_map[f"p{idx}"] = asr_text

        input_data["split_prompts"] = split_prompts
        input_data["prompt_asr_map"] = prompt_asr_map

        # Step 2: 机械生成所有可能的 combo
        prompt_keys = list(split_prompts.keys())
        all_possible: list[dict] = []
        for key in prompt_keys:
            for mid in model_ids:
                all_possible.append({
                    "combo_id": key,
                    "label": key,
                    "prompt_wav_paths": [split_prompts[key]],
                    "model_id": mid,
                    "params": params_map.get(mid, {}),
                    "priority": len(all_possible) + 1,
                    "reasoning": "",
                })
        if len(prompt_keys) >= 2:
            for i in range(len(prompt_keys) - 1):
                k1, k2 = prompt_keys[i], prompt_keys[i + 1]
                combo_id = f"{k1}+{k2}"
                for mid in model_ids:
                    all_possible.append({
                        "combo_id": combo_id,
                        "label": f"{k1}+{k2}",
                        "prompt_wav_paths": [split_prompts[k1], split_prompts[k2]],
                        "model_id": mid,
                        "params": params_map.get(mid, {}),
                        "priority": len(all_possible) + 1,
                        "reasoning": "",
                    })

        # Step 3: 查询知识库（同 context_key 历史记录）
        kb_records: list[dict] = []
        try:
            from sqlalchemy import func as sqlfunc
            rows = (
                db.query(
                    PromptExperiment.prompt_combo,
                    PromptExperiment.model_id,
                    sqlfunc.avg(PromptExperiment.human_score).label("avg_score"),
                    sqlfunc.count(PromptExperiment.id).label("n_samples"),
                )
                .filter(PromptExperiment.business_context_key == context_key)
                .group_by(PromptExperiment.prompt_combo, PromptExperiment.model_id)
                .all()
            )
            kb_records = [
                {
                    "prompt_combo": r.prompt_combo,
                    "model_id": r.model_id,
                    "avg_score": round(float(r.avg_score), 2) if r.avg_score is not None else None,
                    "n_samples": r.n_samples,
                }
                for r in rows
            ]
        except Exception as exc:
            task.append_log(f"知识库查询失败（{exc}）", "warn")

        input_data["kb_query_result"] = {
            "query_key": context_key,
            "records": kb_records,
        }
        task.append_log(
            f"知识库：key={context_key!r}，找到 {len(kb_records)} 条历史记录"
        )
        db.commit()

        # Step 4: Claude 推理（为每个 combo 生成 reasoning 并调整优先级）
        combos = all_possible
        agent_reasoning = ""
        client = _build_client()
        if client and all_possible:
            try:
                prompts_desc = "\n".join(
                    f"  {k}: ASR=\"{prompt_asr_map.get(k, '未知')[:60]}\""
                    for k in prompt_keys
                )
                combos_desc = "\n".join(
                    f"  {i+1}. combo_id={c['combo_id']}, model_id={c['model_id']}"
                    for i, c in enumerate(all_possible)
                )
                if kb_records:
                    kb_desc = "\n".join(
                        f"  combo={r['prompt_combo']}, model={r['model_id']}, "
                        f"均分={r['avg_score']}, 样本数={r['n_samples']}"
                        for r in kb_records
                    )
                    kb_section = (
                        f"【知识库历史（同场景 context_key={context_key!r}）】\n{kb_desc}"
                    )
                    prior_note = (
                        "请结合历史数据判断优先测试哪些组合，历史均分高的组合可适当提高优先级。"
                    )
                else:
                    kb_section = "【知识库历史】暂无同场景历史数据"
                    prior_note = (
                        "由于没有历史数据，请建议对所有组合进行全量测试，"
                        "并在 reasoning 中标注「无先验数据，建议全量测试」。"
                    )

                ctx = business_context
                reasoning_prompt = (
                    f"你是语音克隆实验设计专家，请为每个测试组合写出推荐理由。\n\n"
                    f"【5D 场景标签】"
                    f"角色={ctx.get('character_type', '')} "
                    f"情感={ctx.get('emotion_register', '')} "
                    f"内容={ctx.get('content_type', '')} "
                    f"风格={ctx.get('language_style', '')}\n\n"
                    f"【音频素材（ASR 转写）】\n{prompts_desc}\n\n"
                    f"{kb_section}\n\n"
                    f"【所有可能的测试组合】\n{combos_desc}\n\n"
                    f"{prior_note}\n\n"
                    f"请为每个组合写出 1-2 句推荐理由（含具体依据），"
                    f"并按推荐程度分配 priority（从1开始，越小越优先）。\n"
                    f"仅返回 JSON 数组，每项格式：\n"
                    f'{{\"combo_id\": \"p0\", \"model_id\": \"fish-audio\", '
                    f'\"priority\": 1, \"reasoning\": \"...\"}}'
                )
                raw = _call_claude(client, reasoning_prompt, max_tokens=2048)
                s, e2 = raw.find("["), raw.rfind("]")
                if s != -1 and e2 != -1:
                    reasoning_list = json.loads(raw[s:e2 + 1])
                    reason_lookup = {
                        f"{r.get('combo_id')}_{r.get('model_id')}": r
                        for r in reasoning_list
                        if isinstance(r, dict)
                    }
                    for c in combos:
                        lk = f"{c['combo_id']}_{c['model_id']}"
                        if lk in reason_lookup:
                            c["reasoning"] = reason_lookup[lk].get("reasoning", "")
                            c["priority"] = reason_lookup[lk].get("priority", c["priority"])
                    combos.sort(key=lambda x: x.get("priority", 99))
                    agent_reasoning = f"已为 {len(reasoning_list)} 个组合生成推理说明"
                    task.append_log(f"Claude 推理完成：{agent_reasoning}")
            except Exception as exc:
                task.append_log(f"Claude 推理失败（{exc}），使用默认排序", "warn")
                if not kb_records:
                    for c in combos:
                        if not c.get("reasoning"):
                            c["reasoning"] = "无先验数据，建议全量测试"
        else:
            if not kb_records:
                for c in combos:
                    c["reasoning"] = "无先验数据，建议全量测试"

        input_data["combo_plan"] = combos
        input_data["agent_reasoning"] = agent_reasoning
        task.append_log(f"生成 {len(combos)} 个实验组合")
        db.commit()
        self._pause(task, input_data, "confirm_plan", db)

    # ──────────────────────────────────────────────────────────────────────
    # Phase 3：内联合成所有 combo × 所有文本
    # ──────────────────────────────────────────────────────────────────────
    def _phase_confirm_plan(self, task, input_data: dict, db: Session) -> None:
        confirmed_plan = input_data.get("confirmed_plan", [])
        if not confirmed_plan:
            raise ValueError("confirmed_plan 不能为空")

        confirmed_texts = input_data.get("confirmed_texts", [])
        task.append_log(
            f"Phase confirm_plan: {len(confirmed_plan)} 个 combo × {len(confirmed_texts)} 条文本，开始合成"
        )
        db.commit()

        import backend.executors.subprocess_exec  # noqa
        import backend.executors.sglang_exec      # noqa
        import backend.executors.api_exec         # noqa
        from backend.executors.base import ExecutorFactory
        from collections import defaultdict

        synthesis_results: list[dict] = input_data.get("synthesis_results", [])
        # Fix: use (combo_id, model_id) as resume key to avoid cross-model skipping
        done_combo_keys: set[tuple] = {(r["combo_id"], r["model_id"]) for r in synthesis_results}
        n_texts = len(confirmed_texts)
        total_items = len(confirmed_plan) * n_texts
        done_items = len(done_combo_keys) * n_texts

        (OUTPUTS_BASE / task.id).mkdir(parents=True, exist_ok=True)

        # Group by model_id: load each model once, run all its combos before unloading
        by_model: dict[str, list[dict]] = defaultdict(list)
        for item in confirmed_plan:
            by_model[item["model_id"]].append(item)

        for model_id, model_combos in by_model.items():
            model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
            if model is None or model.status != "active":
                for plan_item in model_combos:
                    cid = plan_item["combo_id"]
                    if (cid, model_id) in done_combo_keys:
                        continue
                    task.append_log(f"[{cid}_{model_id}] 模型不可用，跳过", "warn")
                    done_items += n_texts
                db.commit()
                continue

            for plan_item in model_combos:
                combo_id = plan_item["combo_id"]
                if (combo_id, model_id) in done_combo_keys:
                    continue

                combo_label = f"{combo_id}_{model_id}"
                prompt_wav_paths = plan_item.get("prompt_wav_paths", [])
                params = plan_item.get("params", {})

                # 拼接该 combo 对应的 ASR 参考文本（cosyvoice2 zero-shot 必须）
                prompt_asr_map: dict[str, str] = input_data.get("prompt_asr_map", {})
                combo_parts = combo_id.split("+")
                source_text = " ".join(
                    prompt_asr_map.get(p, "") for p in combo_parts
                ).strip()

                tmp_combined = None
                if len(prompt_wav_paths) > 1:
                    tmp_combined = f"/tmp/{task.id}_{combo_id.replace('+', '_')}_combined.wav"
                    concat_wavs(prompt_wav_paths, tmp_combined)
                    prompt_path = tmp_combined
                else:
                    prompt_path = prompt_wav_paths[0] if prompt_wav_paths else ""

                tmp_jsonl = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
                )
                for i, text in enumerate(confirmed_texts):
                    tmp_jsonl.write(
                        json.dumps(
                            {"key": f"t{i}", "text": text, "source_path": "", "source_text": source_text},
                            ensure_ascii=False,
                        ) + "\n"
                    )
                tmp_jsonl.close()

                sub_input = {
                    "model_id": model_id,
                    "jsonl_path": tmp_jsonl.name,
                    "global_prompt_path": prompt_path,
                    "prompt_map": {},
                    "params": params,
                    "parent_task_id": task.id,
                    "label": combo_label,
                }
                sub_task = Task(
                    id=str(uuid.uuid4()),
                    type="tts",
                    status="running",
                    input=json.dumps(sub_input, ensure_ascii=False),
                    logs="[]",
                )
                db.add(sub_task)
                db.commit()

                try:
                    executor = ExecutorFactory.get(model.model_type)
                    result = executor.execute(sub_task, model, db)
                    sub_task.status = "success"
                    sub_task.result = json.dumps(result.to_dict(), ensure_ascii=False)
                    sub_task.finished_at = datetime.utcnow()

                    for item in result.items:
                        if item["status"] == "success":
                            t_idx = item["key"][1:]
                            idx_int = int(t_idx)
                            synthesis_results.append({
                                "text_idx": t_idx,
                                "text": confirmed_texts[idx_int] if idx_int < len(confirmed_texts) else "",
                                "combo_id": combo_id,
                                "model_id": model_id,
                                "audio_url": item["audio_url"],
                                "sub_task_id": sub_task.id,
                            })
                    done_items += len(result.items)

                except Exception as exc:
                    sub_task.status = "failed"
                    sub_task.error = str(exc)
                    sub_task.finished_at = datetime.utcnow()
                    task.append_log(f"[{combo_label}] 合成失败: {exc}", "error")
                    done_items += n_texts

                db.commit()  # save sub_task final state

                # Forward per-item logs from sub-task to parent experiment log
                try:
                    sub_logs = json.loads(sub_task.logs or "[]")
                    for entry in sub_logs:
                        msg = entry.get("msg", "")
                        level = entry.get("level", "info")
                        # Only forward item-progress lines: "[N/M] tK: ok (Xs)"
                        if msg.startswith("[") and "/" in msg:
                            task.append_log(f"[{combo_label}] {msg}", level)
                except Exception:
                    pass

                task.append_log(f"[{combo_label}] 完成 [{done_items}/{total_items} 条]")
                db.commit()  # commit logs before saving input data

                try:
                    os.unlink(tmp_jsonl.name)
                    if tmp_combined:
                        os.unlink(tmp_combined)
                except OSError:
                    pass

                input_data["synthesis_progress"] = {"done": done_items, "total": total_items}
                input_data["synthesis_results"] = synthesis_results
                self._save_phase_data(task, input_data, db)

        self._pause(task, input_data, "select_winner", db)

    # ──────────────────────────────────────────────────────────────────────
    # Phase 4：写入评分数据 + 导出 experiment_result.jsonl
    # ──────────────────────────────────────────────────────────────────────
    def _phase_select_winner(self, task, input_data: dict, db: Session) -> None:
        human_selections: dict = input_data.get("human_selections", {})
        evaluations: dict = input_data.get("evaluations", {})
        synthesis_results: list[dict] = input_data.get("synthesis_results", [])
        confirmed_texts: list[str] = input_data.get("confirmed_texts", [])
        context_key: str = input_data.get("context_key", "")
        business_context: dict = input_data.get("business_context", {})
        confirmed_plan: list[dict] = input_data.get("confirmed_plan", [])

        task.append_log(f"Phase select_winner: 写入 {len(evaluations)} 条评分数据")
        db.commit()

        params_lookup: dict[str, dict] = {
            f"{p['combo_id']}__{p['model_id']}": p.get("params", {})
            for p in confirmed_plan
        }

        for eval_key, eval_data in evaluations.items():
            parts = eval_key.split("__")
            if len(parts) < 3:
                continue
            combo_id, model_id, text_idx = parts[0], parts[1], parts[2]

            sr = next(
                (r for r in synthesis_results
                 if r["combo_id"] == combo_id and r["model_id"] == model_id
                 and r["text_idx"] == text_idx),
                None,
            )
            audio_url = sr["audio_url"] if sr else None
            idx_int = int(text_idx)
            text = sr["text"] if sr else (
                confirmed_texts[idx_int] if idx_int < len(confirmed_texts) else ""
            )

            exp = PromptExperiment(
                task_id=task.id,
                business_context_key=context_key,
                character_type=business_context.get("character_type"),
                emotion_register=business_context.get("emotion_register"),
                content_type=business_context.get("content_type"),
                language_style=business_context.get("language_style"),
                special_req=business_context.get("special_req"),
                prompt_combo=combo_id,
                prompt_hash=_prompt_hash(combo_id),
                model_id=model_id,
                params_json=json.dumps(
                    params_lookup.get(f"{combo_id}__{model_id}", {}),
                    ensure_ascii=False,
                ),
                synthesis_text=text,
                audio_url=audio_url,
                human_score=eval_data.get("score"),
                problem_tags=json.dumps(eval_data.get("problem_tags", []), ensure_ascii=False),
                human_notes=eval_data.get("notes", ""),
            )
            db.add(exp)

        db.commit()

        output_dir = OUTPUTS_BASE / task.id
        output_dir.mkdir(parents=True, exist_ok=True)
        result_jsonl = output_dir / "experiment_result.jsonl"
        winner_config: dict = {}

        with open(result_jsonl, "w", encoding="utf-8") as f:
            for text_idx_str, winner_key in human_selections.items():
                parts = winner_key.split("__")
                if len(parts) < 2:
                    continue
                combo_id, model_id = parts[0], parts[1]
                sr = next(
                    (r for r in synthesis_results
                     if r["combo_id"] == combo_id and r["model_id"] == model_id
                     and r["text_idx"] == text_idx_str),
                    None,
                )
                if sr:
                    f.write(json.dumps({
                        "key": f"t{text_idx_str}",
                        "text": sr["text"],
                        "audio_url": sr["audio_url"],
                        "combo_id": combo_id,
                        "model_id": model_id,
                    }, ensure_ascii=False) + "\n")
                    winner_config[text_idx_str] = {"combo_id": combo_id, "model_id": model_id}

        task.result = json.dumps({
            "jsonl_path": str(result_jsonl),
            "total_texts": len(confirmed_texts),
            "evaluated": len(evaluations),
            "winner_config": winner_config,
        }, ensure_ascii=False)
        task.append_log(f"实验完成，{len(winner_config)} 条 winner 写入 {result_jsonl}")
        db.commit()


AgentExecutorRegistry.register("prompt_experiment", PromptExperimentExecutor)
