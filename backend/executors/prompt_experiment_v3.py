"""克隆实验室 v3 执行器：Agent-Native 多轮迭代。

Phase 状态机：
  start → confirm_asr → task_planning → confirm_plan
  → round_design → confirm_round → synthesis → evaluation → analyze_round
  → (有 winner) write_experience → success
  → (无 winner) round_design (循环)
"""
import json
import uuid
import os
from datetime import datetime, date
from pathlib import Path

from sqlalchemy.orm import Session

from backend.config import (
    ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL,
    ANTHROPIC_MODEL, STORAGE_DIR, OUTPUTS_DIR,
)
from backend.executors.agent_base import AgentBaseExecutor, AgentExecutorRegistry
from backend.tools import asr as asr_tool
from backend.tools import kb_tools
from backend.tools.audio_processing import (
    detect_total_duration, detect_silence_segments,
    trim_silence, concat_audio, split_by_silence,
)

_anthropic_client = None


def _get_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        kwargs = {}
        if ANTHROPIC_BASE_URL:
            kwargs["base_url"] = ANTHROPIC_BASE_URL
        if ANTHROPIC_AUTH_TOKEN:
            kwargs["api_key"] = ANTHROPIC_AUTH_TOKEN
        elif ANTHROPIC_API_KEY:
            kwargs["api_key"] = ANTHROPIC_API_KEY
        _anthropic_client = Anthropic(**kwargs)
    return _anthropic_client


def _call_claude_structured(system: str, user_msg: str, tool_schema: dict, max_tokens: int = 16000,
                            stream_callback=None) -> dict:
    """通过 tool_use 获取结构化输出，支持流式 thinking 输出"""
    import json as _json
    import logging
    _logger = logging.getLogger(__name__)
    client = _get_client()

    if not stream_callback:
        # 非流式路径
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            tools=[{
                "name": "structured_output",
                "description": "输出结构化结果",
                "input_schema": tool_schema,
            }],
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        block_types = [block.type for block in response.content]
        _logger.warning("No tool_use found. Block types: %s, stop_reason: %s", block_types, response.stop_reason)
        text = ""
        for block in response.content:
            if hasattr(block, "text") and block.text:
                text += block.text
        if text:
            text = text.strip()
            return _extract_json(text)
        raise ValueError("No tool_use block or parseable JSON in response")

    # 流式路径
    thinking_text = ""
    content_text = ""
    tool_input_json = ""
    current_type = None

    with client.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
        tools=[{
            "name": "structured_output",
            "description": "输出结构化结果",
            "input_schema": tool_schema,
        }],
    ) as stream:
        for event in stream:
            if event.type == "content_block_start":
                current_type = event.content_block.type
            elif event.type == "content_block_delta":
                if hasattr(event.delta, "thinking") and event.delta.thinking:
                    thinking_text += event.delta.thinking
                    stream_callback(thinking=thinking_text)
                elif hasattr(event.delta, "text") and event.delta.text:
                    content_text += event.delta.text
                elif hasattr(event.delta, "partial_json") and event.delta.partial_json:
                    tool_input_json += event.delta.partial_json

    # 流结束，提取结果
    if tool_input_json:
        return _json.loads(tool_input_json)
    if content_text:
        return _extract_json(content_text.strip())
    raise ValueError("Stream ended without tool_use or parseable JSON")


def _extract_json(text: str) -> dict:
    """从文本中提取 JSON 对象"""
    import json as _json
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        candidate = text.split("```", 1)[1].split("```", 1)[0].strip()
        if candidate.startswith("{"):
            text = candidate
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return _json.loads(text)


def _call_claude_text(system: str, user_msg: str, max_tokens: int = 2000) -> str:
    """普通文本输出"""
    client = _get_client()
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text


# ── JSON Schemas for structured output ──────────────────────────────────────

TASK_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan_summary": {"type": "string"},
        "round1_goal": {"type": "string"},
        "explore_rationale": {"type": "string"},
        "expected_directions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "condition": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["condition", "action"],
            },
        },
        "initial_hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                    "status": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["id", "content", "status"],
            },
        },
        "estimated_rounds": {"type": "string"},
        "cold_start_notice": {"type": ["string", "null"]},
    },
    "required": ["plan_summary", "round1_goal", "initial_hypotheses", "estimated_rounds"],
}

ROUND_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "round": {"type": "integer"},
        "base_texts": {"type": "array", "items": {"type": "string"}},
        "combos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "combo_id": {"type": "string"},
                    "type": {"type": "string", "enum": ["exploit", "diagnostic", "explore"]},
                    "prompt_ids": {"type": "array", "items": {"type": "string"}},
                    "model_id": {"type": "string"},
                    "text_strategy": {"type": "string"},
                    "text_variants": {"type": "object"},
                    "params": {"type": "object"},
                    "reasoning": {"type": "string"},
                    "hypothesis_ref": {"type": ["string", "null"]},
                },
                "required": ["combo_id", "type", "prompt_ids", "model_id", "text_strategy"],
            },
        },
        "hypotheses_this_round": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["id", "content", "status"],
            },
        },
        "plan_summary": {"type": "string"},
    },
    "required": ["round", "base_texts", "combos", "plan_summary"],
}

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnostic_conclusions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hypothesis_id": {"type": "string"},
                    "conclusion": {"type": "string", "enum": ["confirmed", "refuted", "inconclusive"]},
                    "evidence": {"type": "string"},
                },
                "required": ["hypothesis_id", "conclusion", "evidence"],
            },
        },
        "updated_hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["id", "content", "status"],
            },
        },
        "key_findings": {"type": "string"},
        "factor_candidates": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["updated_hypotheses", "key_findings"],
}

# ── System prompt ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """# 角色
你是一个语音克隆策略 agent。你无法听到音频，所有音频信息通过人工文字标注感知。
你的工作不只是"选最好的组合"，而是"设计信息量最大的实验轮次"。

# 模态约束
你无法听音频。所有音频质量判断来源于：
- prompt 标注文件（环境/语速/风格/副语言/人设）
- 用户评价（排名/vs_target/issue 标签/文字备注）
不得基于自己的想象对音频质量做主观判断。

# 核心决策原则
每轮组合同时服务两个目标：
1. 利用（Exploit）：选择历史表现最好的组合，稳定输出
2. 探索（Explore）：填补覆盖矩阵盲区，获取新信息

# 组合分配比例
- exploit 位：多数（基于经验的最佳猜测）
- diagnostic 位：少数（控制变量对照，必须成对：只变一个因子）
- explore 位：1-2 个（覆盖矩阵盲区优先）
注意：每轮总合成数（combos × base_texts）不得超过 30 条。

# Diagnostic 对设计规则
- 每个 diagnostic 对必须明确写出验证的假设 ID
- 控制变量原则：固定其他因子，只变一个（prompt / model / text_strategy）
- diagnostic 对本身必须是有竞争力的组合，不是废棋

# 文本策略设计
- 同一轮内所有组合使用同一批 base_text（内容一致，保证对比公平）
- 不同模型根据各自的 tag_tutorial 生成文本变体（标签格式不同，策略意图相同）

# 评价解读原则
- 排名 > 绝对分数：rank=1 意味着比其他好，不意味着绝对质量足够
- issues 标签 > 数字："[情感平淡]" 比 "3 分" 更有行动指引
- 当前任务历史评价优先级高于 KB 历史经验
- 用户 notes 是最高信息密度来源

# 评分档位语义（5 级 vs_target）
- 不可用（1_unusable）：音频有明显瑕疵，完全不能商用
- 差距明显（2_gap）：能听出目标方向，但差距大，需大幅调整
- 有潜力（3_potential）：部分维度达标，有可取之处，值得进一步调优
- 达到预期（4_expected）：满足商用门槛，可作为最终方案
- 超出预期（5_exceed）：超越目标要求，音质/表现力意外出色

解读规则：
- rank=1 + 评分 ≥ 4_expected = 该组合表现稳定，应 exploit
- rank=1 + 评分 ≤ 3_potential = 虽是本轮最优但整体不足，需继续探索
- 评分 = 1_unusable 的组合应从下轮候选中排除

# 冷启动处理
如果知识库无相关经验：
1. 依赖模型 profile.md 的官方描述
2. 第一轮更发散，多测组合，少做判断
3. 明确标注"首次探索该场景，本轮重在收集数据"
4. 探索位比例提升至 40%

# 假设追踪
每轮输出中维护假设列表：
- status: untested / testing / confirmed / refuted / inconclusive
- 有新证据时更新 status，给出 evidence 字段

# 如果用户选了 winner
立即停止，不再建议追加轮次。"""


class PromptExperimentV3Executor(AgentBaseExecutor):

    def _add_step(self, input_data: dict, task, db: Session, step: str):
        """记录 agent 子步骤，前端侧边栏动态展示"""
        steps = input_data.setdefault("agent_steps", [])
        steps.append({"phase": input_data.get("phase", ""), "text": step, "ts": datetime.utcnow().isoformat()})
        if len(steps) > 50:
            steps[:] = steps[-50:]
        self._save_phase_data(task, input_data, db)

    def _forward_sub_logs(self, parent_task, sub_task, db: Session):
        """将子任务的关键日志转发到父任务（过滤 debug 级别的进度条噪音）"""
        sub_logs = json.loads(sub_task.logs or "[]")
        for log in sub_logs:
            if log.get("level") == "debug":
                continue
            parent_task.append_log(f"[{sub_task.id[:8]}] {log.get('msg', '')}", log.get("level", "info"))
        db.commit()

    # ── Phase 1: start ──────────────────────────────────────────────────────

    def _phase_start(self, task, input_data: dict, db: Session):
        """ASR 转写所有 prompt 音频"""
        task.append_log("开始 ASR 转写 prompt 音频...")
        prompts = input_data.get("prompts", [])

        for i, p in enumerate(prompts):
            if not p.get("asr_text"):
                try:
                    text = asr_tool.transcribe(p["stored_path"])
                    prompts[i]["asr_text"] = text
                    task.append_log(f"Prompt {i}: ASR 完成 → {text[:50]}...")
                except Exception as e:
                    prompts[i]["asr_text"] = ""
                    task.append_log(f"Prompt {i}: ASR 失败 → {e}", "error")

        input_data["prompts"] = prompts
        self._pause(task, input_data, "confirm_asr", db)

    # ── Phase 2: confirm_asr ────────────────────────────────────────────────

    def _phase_confirm_asr(self, task, input_data: dict, db: Session):
        """用户已提交 ASR 校对 + 标注问卷，继续到 task_planning"""
        task.append_log("Prompt 标注已确认，开始任务规划...")
        self._phase_task_planning(task, input_data, db)

    # ── Phase 3: task_planning ──────────────────────────────────────────────

    def _phase_task_planning(self, task, input_data: dict, db: Session):
        """Agent 读 KB → 输出任务级探索计划"""
        input_data["phase"] = "task_planning"
        input_data["agent_steps"] = []
        task.append_log("Agent 正在规划探索策略...")
        input_data["agent_streaming"] = ""
        self._save_phase_data(task, input_data, db)

        self._add_step(input_data, task, db, "读取知识库经验...")
        kb_context = self._read_kb_context(input_data)
        self._add_step(input_data, task, db, "构建规划 Prompt...")
        planning_input = self._build_planning_prompt(input_data, kb_context)

        self._add_step(input_data, task, db, "调用 LLM 生成探索计划...")

        def on_stream(thinking=""):
            input_data["agent_streaming"] = thinking
            self._save_phase_data(task, input_data, db)

        try:
            task_plan = _call_claude_structured(
                SYSTEM_PROMPT, planning_input, TASK_PLAN_SCHEMA, max_tokens=16000,
                stream_callback=on_stream
            )
            self._add_step(input_data, task, db, "解析 LLM 输出...")
        except Exception as e:
            task_plan = {
                "plan_summary": f"规划生成失败: {e}",
                "round1_goal": "广撒网测试所有候选模型",
                "initial_hypotheses": [],
                "estimated_rounds": "2-3 轮",
                "cold_start_notice": "LLM 调用失败，使用默认冷启动策略",
            }
            task.append_log(f"LLM 规划失败，使用默认策略: {e}", "warn")

        input_data["agent_streaming"] = ""
        input_data["task_plan"] = task_plan
        self._save_phase_data(task, input_data, db)
        self._pause(task, input_data, "confirm_plan", db)

    # ── Phase 3b: confirm_plan ──────────────────────────────────────────────

    def _phase_confirm_plan(self, task, input_data: dict, db: Session):
        """用户已确认探索计划，初始化假设，进入 round_design"""
        task_plan = input_data.get("task_plan", {})
        if not input_data.get("hypotheses"):
            input_data["hypotheses"] = task_plan.get("initial_hypotheses", [])
        task.append_log("探索计划已确认，进入首轮设计...")
        self._phase_round_design(task, input_data, db)

    # ── Phase 4: round_design ───────────────────────────────────────────────

    def _phase_round_design(self, task, input_data: dict, db: Session):
        """Agent 设计本轮组合"""
        round_num = input_data.get("round", 0) + 1
        input_data["round"] = round_num
        input_data["phase"] = "round_design"
        input_data["agent_steps"] = []
        task.append_log(f"Agent 正在设计第 {round_num} 轮实验组合...")
        input_data["agent_streaming"] = ""
        self._save_phase_data(task, input_data, db)

        self._add_step(input_data, task, db, "读取知识库经验...")
        kb_context = self._read_kb_context(input_data)
        self._add_step(input_data, task, db, "构建轮次设计 Prompt...")
        agent_input = self._build_round_design_prompt(input_data, kb_context)

        self._add_step(input_data, task, db, "调用 LLM 设计组合...")

        def on_stream(thinking=""):
            input_data["agent_streaming"] = thinking
            self._save_phase_data(task, input_data, db)

        try:
            round_plan = _call_claude_structured(
                SYSTEM_PROMPT, agent_input, ROUND_PLAN_SCHEMA, max_tokens=16000,
                stream_callback=on_stream
            )
            round_plan["round"] = round_num
            self._add_step(input_data, task, db, "解析组合方案...")
        except Exception as e:
            round_plan = self._fallback_round_plan(input_data, round_num)
            task.append_log(f"LLM 轮次设计失败，使用回退方案: {e}", "warn")

        input_data["agent_streaming"] = ""
        input_data["current_round_plan"] = round_plan
        self._save_phase_data(task, input_data, db)
        self._pause(task, input_data, "confirm_round", db)

    # ── Phase 5: confirm_round ──────────────────────────────────────────────

    def _phase_confirm_round(self, task, input_data: dict, db: Session):
        """用户已确认组合列表，开始合成"""
        task.append_log("组合已确认，开始批量合成...")
        self._phase_synthesis(task, input_data, db)

    # ── Phase 6: synthesis ──────────────────────────────────────────────────

    def _phase_synthesis(self, task, input_data: dict, db: Session):
        """批量合成——按模型分组执行，同模型的任务集中跑完再切换"""
        input_data["phase"] = "synthesis"
        round_plan = input_data.get("current_round_plan", {})
        confirmed_ids = input_data.get("confirmed_combo_ids")
        combos = round_plan.get("combos", [])

        if confirmed_ids:
            combos = [c for c in combos if c["combo_id"] in confirmed_ids]

        base_texts = round_plan.get("base_texts", [])
        if not base_texts:
            base_texts = input_data.get("user_texts", []) or ["测试文本"]

        # 强制约束：总数 ≤ 30
        total = len(combos) * len(base_texts)
        if total > 30:
            combos = combos[:30 // max(len(base_texts), 1)]
            total = len(combos) * len(base_texts)
            task.append_log(f"约束裁剪: combo 数调整为 {len(combos)}，总合成数 {total}")

        synthesis_results = input_data.get("synthesis_results", [])
        done = 0
        input_data["agent_steps"] = []
        input_data["synthesis_progress"] = {"done": 0, "total": total}
        self._save_phase_data(task, input_data, db)

        task_output_dir = OUTPUTS_DIR / task.id
        task_output_dir.mkdir(parents=True, exist_ok=True)

        # 按 model_id 分组，同模型集中执行
        from collections import OrderedDict
        model_groups: OrderedDict[str, list] = OrderedDict()
        for combo in combos:
            mid = combo.get("model_id", "")
            model_groups.setdefault(mid, []).append(combo)

        for model_id, group_combos in model_groups.items():
            self._add_step(input_data, task, db, f"加载模型 {model_id}...")
            task.append_log(f"── 模型 {model_id}：{len(group_combos)} 组合 × {len(base_texts)} 文本 ──")
            db.commit()

            # 收集同模型所有 items，一次性批量合成
            batch_items = []
            batch_meta = []  # 与 batch_items 一一对应的元数据
            for combo in group_combos:
                prompt_ids = combo.get("prompt_ids", [])
                text_variants = combo.get("text_variants", {})
                combo_params = combo.get("params", {})
                processed_prompt = self._process_prompt_audio(input_data, prompt_ids)

                prompts = input_data.get("prompts", [])
                source_text = ""
                for pid in prompt_ids:
                    idx = int(pid.replace("p", ""))
                    if idx < len(prompts):
                        source_text += prompts[idx].get("asr_text", "") + " "
                source_text = source_text.strip()

                scene_form = input_data.get("scene_form", {})
                context_prompt = scene_form.get("context_prompt", "")

                for text_idx, base_text in enumerate(base_texts):
                    text_variant = text_variants.get(model_id, base_text)
                    combo_id = combo.get("combo_id", f"p{'_'.join(prompt_ids)}_{model_id}")
                    audio_label = f"t{text_idx}_{combo_id}"

                    batch_items.append({
                        "key": audio_label,
                        "text": text_variant,
                        "source_text": source_text,
                        "source_path": processed_prompt,
                        "context_prompt": context_prompt,
                        **combo_params,
                    })
                    batch_meta.append({
                        "combo_id": combo_id,
                        "combo": combo,
                        "text_idx": text_idx,
                        "text": text_variant,
                        "model_id": model_id,
                        "prompt_ids": prompt_ids,
                        "audio_label": audio_label,
                    })

            # 创建一个子任务跑整批
            try:
                sub_task_id = self._synthesize_batch(task, input_data, db, model_id, batch_items)
                # 批量成功，生成结果条目
                for meta in batch_meta:
                    synthesis_results.append({
                        "combo_id": meta["combo_id"],
                        "round": input_data["round"],
                        "text_idx": meta["text_idx"],
                        "text": meta["text"],
                        "model_id": meta["model_id"],
                        "prompt_ids": meta["prompt_ids"],
                        "audio_url": f"/storage/outputs/{sub_task_id}/{meta['audio_label']}.wav",
                        "audio_label": meta["audio_label"],
                        "sub_task_id": sub_task_id,
                        "status": "success",
                        "error_reason": None,
                    })
                    done += 1
                    input_data["synthesis_progress"] = {"done": done, "total": total}
                input_data["synthesis_results"] = synthesis_results
                self._save_phase_data(task, input_data, db)
            except Exception as e:
                task.append_log(f"模型 {model_id} 批量合成失败: {e}", "error")
                for meta in batch_meta:
                    synthesis_results.append({
                        "combo_id": meta["combo_id"],
                        "round": input_data["round"],
                        "text_idx": meta["text_idx"],
                        "text": meta["text"],
                        "model_id": meta["model_id"],
                        "prompt_ids": meta["prompt_ids"],
                        "audio_url": None,
                        "audio_label": meta["audio_label"],
                        "sub_task_id": None,
                        "status": "failed",
                        "error_reason": str(e),
                    })
                    done += 1
                input_data["synthesis_progress"] = {"done": done, "total": total}
                input_data["synthesis_results"] = synthesis_results
                self._save_phase_data(task, input_data, db)

        task.append_log(f"合成完成: {done}/{total}，成功 {sum(1 for r in synthesis_results if r.get('status') == 'success')} 条")
        input_data["synthesis_results"] = synthesis_results
        self._pause(task, input_data, "evaluation", db)

    # ── Phase 7: evaluation ─────────────────────────────────────────────────

    def _phase_evaluation(self, task, input_data: dict, db: Session):
        """用户已提交排名评价，继续到 analyze_round"""
        task.append_log("评价已提交，Agent 正在分析...")
        self._phase_analyze_round(task, input_data, db)

    # ── Phase 8: analyze_round ──────────────────────────────────────────────

    def _phase_analyze_round(self, task, input_data: dict, db: Session):
        """Agent 分析本轮评价"""
        pending_eval = input_data.get("pending_evaluation", {})

        if pending_eval.get("winner"):
            task.append_log(f"用户选出 Winner: {pending_eval['winner']}，正在写入经验...")
            self._phase_write_experience(task, input_data, db)
            return

        input_data["phase"] = "analyze_round"
        input_data["agent_steps"] = []
        task.append_log("无 Winner，Agent 分析归因中...")
        input_data["agent_streaming"] = ""
        self._save_phase_data(task, input_data, db)

        self._add_step(input_data, task, db, "构建分析 Prompt...")
        analysis_prompt = self._build_analysis_prompt(input_data)
        self._add_step(input_data, task, db, "调用 LLM 分析评价...")

        def on_stream(thinking=""):
            input_data["agent_streaming"] = thinking
            self._save_phase_data(task, input_data, db)

        try:
            analysis = _call_claude_structured(
                SYSTEM_PROMPT, analysis_prompt, ANALYSIS_SCHEMA, max_tokens=16000,
                stream_callback=on_stream
            )
            self._add_step(input_data, task, db, "解析分析结果...")
        except Exception as e:
            analysis = {
                "updated_hypotheses": input_data.get("hypotheses", []),
                "key_findings": f"分析失败: {e}",
                "diagnostic_conclusions": [],
                "factor_candidates": [],
            }
            task.append_log(f"LLM 分析失败: {e}", "warn")

        task_history = input_data.get("task_history", [])
        task_history.append({
            "round": input_data.get("round", 1),
            "plan": input_data.get("current_round_plan", {}),
            "eval": pending_eval,
            "analysis": analysis,
        })
        input_data["task_history"] = task_history
        input_data["hypotheses"] = analysis.get("updated_hypotheses", [])
        input_data["agent_streaming"] = ""
        input_data.pop("current_round_plan", None)
        input_data.pop("pending_evaluation", None)
        input_data.pop("synthesis_results", None)
        input_data.pop("synthesis_progress", None)
        input_data.pop("confirmed_combo_ids", None)

        task.append_log(f"第 {input_data.get('round', 1)} 轮分析完成: {analysis.get('key_findings', '')[:80]}")
        self._save_phase_data(task, input_data, db)
        self._pause(task, input_data, "round_design", db)

    # ── Phase 9: write_experience ───────────────────────────────────────────

    def _phase_write_experience(self, task, input_data: dict, db: Session):
        """写 KB 经验文件 + 更新覆盖矩阵 → success"""
        from backend.models.task import SynthesisRecord

        pending_eval = input_data.get("pending_evaluation", {})
        winner_id = pending_eval.get("winner", "")
        scene_type = input_data.get("scene_type", "其他")

        # 写 SynthesisRecord
        self._write_synthesis_records(task, input_data, pending_eval, db)

        # 生成经验文件内容
        experience_content = self._build_experience_content(input_data, pending_eval)
        exp_path = kb_tools.write_experience_file(task.id, scene_type, experience_content)
        input_data["experience_file_path"] = exp_path
        task.append_log(f"经验已写入: {exp_path}")

        # 更新覆盖矩阵
        if winner_id:
            winner_combo = self._find_winner_combo(input_data, winner_id)
            if winner_combo:
                kb_tools.update_coverage_matrix(
                    scene_type=scene_type,
                    model_id=winner_combo.get("model_id", ""),
                    avg_rank=1.0,
                    n_rounds=input_data.get("round", 1),
                )

        # 构建 winner config
        winner_config = self._build_winner_config(input_data, winner_id)
        task.result = json.dumps({
            "winner_config": winner_config,
            "experience_path": exp_path,
            "total_rounds": input_data.get("round", 1),
        }, ensure_ascii=False)

        task.append_log("克隆实验完成！")
        input_data["phase"] = "success"
        task.input = json.dumps(input_data, ensure_ascii=False)
        task.status = "success"
        task.finished_at = datetime.utcnow()
        db.commit()

    # ── Helper methods ──────────────────────────────────────────────────────

    def _read_kb_context(self, input_data: dict) -> str:
        scene_type = input_data.get("scene_type", "")
        model_ids = input_data.get("model_ids", [])
        parts = []

        # 覆盖度矩阵
        coverage = kb_tools.check_coverage(scene_type, model_ids)
        parts.append(f"## 覆盖度\n{coverage['summary']}")
        if coverage["gaps"]:
            parts.append("盲区: " + ", ".join(f"{g['model_id']}/{g['scene_type']}" for g in coverage["gaps"]))

        # 模型 profiles
        for mid in model_ids:
            profile = kb_tools.read_model_profile(mid)
            if profile["profile"]:
                parts.append(f"## 模型 {mid}\n{profile['profile'][:500]}")
            if profile["tag_tutorial"]:
                parts.append(f"### {mid} tag_tutorial\n{profile['tag_tutorial'][:500]}")

        # 相关因子
        if scene_type:
            factors = kb_tools.grep_kb(scene_type, ["factors"])
            if factors:
                parts.append("## 相关因子")
                for f in factors[:5]:
                    parts.append(f"- {f['file']}: {f['snippet'][:200]}")

        # 相关经验
        experiences = kb_tools.list_experiences(scene_type, limit=3)
        if experiences:
            parts.append("## 相关历史经验")
            for ep in experiences:
                content = kb_tools.read_kb_file(ep)
                parts.append(f"### {ep}\n{content[:600]}")

        return "\n\n".join(parts)

    def _build_planning_prompt(self, input_data: dict, kb_context: str) -> str:
        prompts_info = []
        for i, p in enumerate(input_data.get("prompts", [])):
            info = f"Prompt {i} (p{i}): ASR=\"{p.get('asr_text', '')}\""
            ann = p.get("annotation", {})
            if ann:
                info += f"\n  标注: 风格={ann.get('style','')}, 人设={ann.get('character','')}, 环境={ann.get('recording_env','')}"
            prompts_info.append(info)

        return f"""## 任务信息
场景类型: {input_data.get('scene_type', '')}
场景描述: {input_data.get('scene_description', '')}
候选模型: {', '.join(input_data.get('model_ids', []))}

## Prompt 音频
{chr(10).join(prompts_info)}

## 知识库上下文
{kb_context}

## 用户补充方向
{input_data.get('user_direction', '无')}

请输出任务级探索计划（TaskPlan），包括首轮目标、初始假设、预估轮数。"""

    def _build_round_design_prompt(self, input_data: dict, kb_context: str) -> str:
        round_num = input_data.get("round", 1)
        hypotheses = input_data.get("hypotheses", [])
        task_history = input_data.get("task_history", [])
        user_texts = input_data.get("user_texts", [])

        parts = [
            f"## 当前第 {round_num} 轮",
            f"场景: {input_data.get('scene_type', '')} - {input_data.get('scene_description', '')}",
            f"可用模型（测试范围，不必全用，根据场景选择合适的）: {', '.join(input_data.get('model_ids', []))}",
            f"可用 Prompt: p0..p{len(input_data.get('prompts', [])) - 1}",
        ]

        if user_texts:
            parts.append(f"用户提供的 base_texts: {json.dumps(user_texts, ensure_ascii=False)}")
        else:
            parts.append("需要自行生成 base_texts（适合场景的中文测试句）")

        parts.append("""
## 硬性约束（必须遵守）
- 每组 base_text 对应的 combo 数量 ≤ 15（同一文本最多产生 15 条音频）
- 若无用户提供文本（纯自动生成），base_texts 最多 5 条
- 本轮总合成数 = len(combos) × len(base_texts) ≤ 30
- 一组 base_text 不可拆分到不同轮次
- 推荐组合：10 combos × 3 texts 或 15 combos × 2 texts
- 自由决定具体的 combo 和 text 数量，不需要对称""")

        parts.append(f"\n## 知识库上下文\n{kb_context}")

        if hypotheses:
            parts.append("\n## 当前假设")
            for h in hypotheses:
                parts.append(f"- [{h.get('status','?')}] {h.get('id','')}: {h.get('content','')}")

        if task_history:
            parts.append("\n## 历史轮次")
            for hist in task_history[-3:]:
                r = hist.get("round", "?")
                findings = hist.get("analysis", {}).get("key_findings", "")
                parts.append(f"Round {r}: {findings}")

        user_dir = input_data.get("pending_evaluation", {}).get("next_round_direction", "")
        if user_dir:
            parts.append(f"\n## 用户期望方向\n{user_dir}")

        parts.append(f"\n请设计第 {round_num} 轮组合（RoundPlan）。")
        return "\n".join(parts)

    def _build_analysis_prompt(self, input_data: dict) -> str:
        pending_eval = input_data.get("pending_evaluation", {})
        round_plan = input_data.get("current_round_plan", {})
        hypotheses = input_data.get("hypotheses", [])

        parts = [
            f"## 第 {input_data.get('round', 1)} 轮分析",
            f"\n## 本轮组合设计\n{json.dumps(round_plan.get('combos', []), ensure_ascii=False, indent=2)[:2000]}",
            f"\n## 用户排名评价\n{json.dumps(pending_eval.get('rankings_by_text', {}), ensure_ascii=False, indent=2)[:1500]}",
            f"\n## 用户逐条评价\n{json.dumps(pending_eval.get('per_combo', {}), ensure_ascii=False, indent=2)[:1500]}",
        ]

        if hypotheses:
            parts.append("\n## 当前假设")
            for h in hypotheses:
                parts.append(f"- [{h.get('status', '?')}] {h.get('id', '')}: {h.get('content', '')}")

        user_notes = pending_eval.get("next_round_direction", "")
        if user_notes:
            parts.append(f"\n## 用户备注\n{user_notes}")

        parts.append("\n请分析本轮实验结果，更新假设，总结发现。")
        return "\n".join(parts)

    def _fallback_round_plan(self, input_data: dict, round_num: int) -> dict:
        """LLM 失败时的回退方案：简单枚举"""
        model_ids = input_data.get("model_ids", [])
        prompts = input_data.get("prompts", [])
        user_texts = input_data.get("user_texts", [])
        base_texts = user_texts if user_texts else ["这是一条测试文本"]

        combos = []
        idx = 0
        for mid in model_ids[:2]:
            for pi in range(min(len(prompts), 2)):
                idx += 1
                combos.append({
                    "combo_id": f"r{round_num}_c{idx}",
                    "type": "exploit" if idx <= 2 else "explore",
                    "prompt_ids": [f"p{pi}"],
                    "model_id": mid,
                    "text_strategy": "无标签",
                    "text_variants": {mid: base_texts[0]},
                    "params": {},
                    "reasoning": "LLM 回退方案：基础枚举",
                    "hypothesis_ref": None,
                })

        return {
            "round": round_num,
            "base_texts": base_texts,
            "combos": combos,
            "plan_summary": "LLM 不可用，使用回退枚举方案",
        }

    def _process_prompt_audio(self, input_data: dict, prompt_ids: list[str]) -> str:
        """处理 prompt 音频：单条直接 trim，多条按规则拼接或独立"""
        prompts = input_data.get("prompts", [])
        paths = []
        for pid in prompt_ids:
            idx = int(pid.replace("p", ""))
            if idx < len(prompts):
                paths.append(prompts[idx]["stored_path"])

        if not paths:
            return ""

        if len(paths) == 1:
            return trim_silence(paths[0])

        total_dur = sum(detect_total_duration(p) for p in paths)
        if total_dur > 60:
            return trim_silence(paths[0])

        return concat_audio(paths, gap_ms=100)

    def _synthesize_batch(self, task, input_data: dict, db: Session,
                          model_id: str, items: list[dict]) -> str:
        """同模型批量合成，返回 sub_task.id（音频文件都在该目录下）"""
        from backend.models.task import Task as TaskModel, ModelRegistry
        from backend.executors.base import ExecutorFactory
        import backend.executors.subprocess_exec  # noqa
        import backend.executors.sglang_exec      # noqa
        import backend.executors.api_exec         # noqa

        model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
        if not model or model.status != "active":
            raise ValueError(f"模型 {model_id} 不可用")

        sub_input = {
            "model_id": model_id,
            "items": items,
            "parent_task_id": task.id,
        }

        sub_task = TaskModel(
            type="tts",
            status="pending",
            input=json.dumps(sub_input, ensure_ascii=False),
        )
        db.add(sub_task)
        db.commit()

        sub_task.status = "running"
        db.commit()

        # 写入 current_sub_task_id 供前端实时轮询子任务日志
        input_data["current_sub_task_id"] = sub_task.id
        self._save_phase_data(task, input_data, db)

        try:
            executor = ExecutorFactory.get(model.model_type)
            result = executor.execute(sub_task, model, db)
            sub_task.status = "success"
            sub_task.result = json.dumps(result.to_dict(), ensure_ascii=False)
            sub_task.finished_at = datetime.utcnow()
            db.commit()
        except Exception as e:
            sub_task.status = "failed"
            sub_task.error = str(e)
            sub_task.finished_at = datetime.utcnow()
            db.commit()
            self._forward_sub_logs(task, sub_task, db)
            raise

        self._forward_sub_logs(task, sub_task, db)
        task.append_log(f"模型 {model_id} 批次完成: {result.ok} 成功 / {result.fail} 失败")
        db.commit()
        return sub_task.id

    def _synthesize_one(self, task, input_data: dict, db: Session,
                        combo: dict, text_idx: int, text: str,
                        prompt_path: str, audio_label: str,
                        output_dir: Path, combo_params: dict) -> dict:
        """合成单条音频，通过创建 sub_task 调用现有执行器"""
        from backend.models.task import Task as TaskModel, ModelRegistry
        from backend.executors.base import ExecutorFactory
        import backend.executors.subprocess_exec  # noqa
        import backend.executors.sglang_exec      # noqa
        import backend.executors.api_exec         # noqa

        model_id = combo.get("model_id", "")
        model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
        if not model or model.status != "active":
            raise ValueError(f"模型 {model_id} 不可用")

        # 构建合成参数
        prompts = input_data.get("prompts", [])
        prompt_ids = combo.get("prompt_ids", [])
        source_text = ""
        for pid in prompt_ids:
            idx = int(pid.replace("p", ""))
            if idx < len(prompts):
                source_text += prompts[idx].get("asr_text", "") + " "
        source_text = source_text.strip()

        scene_form = input_data.get("scene_form", {})
        context_prompt = scene_form.get("context_prompt", "")

        sub_input = {
            "model_id": model_id,
            "items": [{
                "key": audio_label,
                "text": text,
                "source_text": source_text,
                "source_path": prompt_path,
                "context_prompt": context_prompt,
                **combo_params,
            }],
            "parent_task_id": task.id,
        }

        sub_task = TaskModel(
            type="tts",
            status="pending",
            input=json.dumps(sub_input, ensure_ascii=False),
        )
        db.add(sub_task)
        db.commit()

        sub_task.status = "running"
        db.commit()

        try:
            executor = ExecutorFactory.get(model.model_type)
            result = executor.execute(sub_task, model, db)
            sub_task.status = "success"
            sub_task.result = json.dumps(result.to_dict(), ensure_ascii=False)
            sub_task.finished_at = datetime.utcnow()
            db.commit()
        except Exception as e:
            sub_task.status = "failed"
            sub_task.error = str(e)
            sub_task.finished_at = datetime.utcnow()
            db.commit()
            raise

        audio_url = f"/storage/outputs/{sub_task.id}/{audio_label}.wav"
        return {
            "combo_id": combo["combo_id"],
            "round": input_data["round"],
            "text_idx": text_idx,
            "text": text,
            "model_id": model_id,
            "prompt_ids": prompt_ids,
            "audio_url": audio_url,
            "audio_label": audio_label,
            "sub_task_id": sub_task.id,
            "status": "success",
            "error_reason": None,
        }

    def _write_synthesis_records(self, task, input_data: dict, pending_eval: dict, db: Session):
        """将评价写入 SynthesisRecord 表"""
        from backend.models.task import SynthesisRecord

        rankings_by_text = pending_eval.get("rankings_by_text", {})
        per_combo = pending_eval.get("per_combo", {})
        winner_id = pending_eval.get("winner", "")
        synthesis_results = input_data.get("synthesis_results", [])

        for sr in synthesis_results:
            if sr.get("status") != "success":
                continue

            combo_id = sr["combo_id"]
            text_idx = str(sr["text_idx"])
            eval_key = f"{combo_id}_t{text_idx}"

            rank = None
            text_rankings = rankings_by_text.get(text_idx, [])
            for r in text_rankings:
                if r.get("combo_id") == combo_id:
                    rank = r.get("rank")
                    break

            combo_eval = per_combo.get(eval_key, {})

            record = SynthesisRecord(
                task_id=task.id,
                scene_type=input_data.get("scene_type"),
                scene_description=input_data.get("scene_description"),
                round_number=sr.get("round", 1),
                combo_id=combo_id,
                combo_type=self._get_combo_type(input_data, combo_id),
                prompt_ids=json.dumps(sr.get("prompt_ids", [])),
                model_id=sr.get("model_id", ""),
                text_strategy=self._get_text_strategy(input_data, combo_id),
                synthesis_text=sr.get("text", ""),
                audio_url=sr.get("audio_url"),
                sub_task_id=sr.get("sub_task_id"),
                human_rank=rank,
                vs_target=combo_eval.get("vs_target"),
                issue_tags=json.dumps(combo_eval.get("issues", [])),
                human_notes=combo_eval.get("notes"),
                is_winner=(sr.get("audio_label") == winner_id),
            )
            db.add(record)

        db.commit()

    def _get_combo_type(self, input_data: dict, combo_id: str) -> str | None:
        plan = input_data.get("current_round_plan", {})
        for c in plan.get("combos", []):
            if c["combo_id"] == combo_id:
                return c.get("type")
        return None

    def _get_text_strategy(self, input_data: dict, combo_id: str) -> str | None:
        plan = input_data.get("current_round_plan", {})
        for c in plan.get("combos", []):
            if c["combo_id"] == combo_id:
                return c.get("text_strategy")
        return None

    def _find_winner_combo(self, input_data: dict, winner_id: str) -> dict | None:
        for sr in input_data.get("synthesis_results", []):
            if sr.get("audio_label") == winner_id:
                plan = input_data.get("current_round_plan", {})
                for c in plan.get("combos", []):
                    if c["combo_id"] == sr["combo_id"]:
                        return {**c, "audio_url": sr.get("audio_url")}
        return None

    def _build_winner_config(self, input_data: dict, winner_id: str) -> dict:
        """构建 winner 的完整配置，供批量合成页预填"""
        winner_combo = self._find_winner_combo(input_data, winner_id)
        if not winner_combo:
            return {}

        prompts = input_data.get("prompts", [])
        prompt_ids = winner_combo.get("prompt_ids", [])
        prompt_paths = []
        for pid in prompt_ids:
            idx = int(pid.replace("p", ""))
            if idx < len(prompts):
                prompt_paths.append(prompts[idx]["stored_path"])

        return {
            "model_id": winner_combo.get("model_id", ""),
            "prompt_ids": prompt_ids,
            "prompt_paths": prompt_paths,
            "text_strategy": winner_combo.get("text_strategy", ""),
            "params": winner_combo.get("params", {}),
            "text_variants": winner_combo.get("text_variants", {}),
            "scene_type": input_data.get("scene_type", ""),
            "scene_form": input_data.get("scene_form", {}),
        }

    def _build_experience_content(self, input_data: dict, pending_eval: dict) -> str:
        """生成经验文件 markdown 内容"""
        winner_id = pending_eval.get("winner", "")
        winner_combo = self._find_winner_combo(input_data, winner_id)
        task_history = input_data.get("task_history", [])

        lines = [
            "---",
            f"scene_type: {input_data.get('scene_type', '')}",
            f"scene_description: {input_data.get('scene_description', '')}",
            f"date: {date.today().isoformat()}",
            f"task_id: {input_data.get('task_id', '')}",
            f"rounds: {input_data.get('round', 1)}",
            f"winner_round: {input_data.get('round', 1)}",
            f"winner_combo: {winner_combo.get('model_id', '')} + {'+'.join(winner_combo.get('prompt_ids', []))} + {winner_combo.get('text_strategy', '')}" if winner_combo else "winner_combo: none",
            "---",
            "",
            "## 轮次记录",
        ]

        for hist in task_history:
            r = hist.get("round", "?")
            n_combos = len(hist.get("plan", {}).get("combos", []))
            findings = hist.get("analysis", {}).get("key_findings", "")
            lines.append(f"\n### Round {r}（{n_combos} 个组合）")
            lines.append(f"Agent 分析: {findings}")

        lines.append(f"\n### Round {input_data.get('round', 1)}（Winner 轮）")
        rankings = pending_eval.get("rankings_by_text", {})
        if rankings:
            first_text_rankings = list(rankings.values())[0] if rankings else []
            for r in sorted(first_text_rankings, key=lambda x: x.get("rank", 99)):
                lines.append(f"- rank {r.get('rank')}: {r.get('combo_id')}")

        lines.append("\n## 结论")
        if winner_combo:
            lines.append(f"- Winner: {winner_combo.get('model_id', '')} + {'+'.join(winner_combo.get('prompt_ids', []))} + {winner_combo.get('text_strategy', '')}")

        hypotheses = input_data.get("hypotheses", [])
        confirmed = [h for h in hypotheses if h.get("status") == "confirmed"]
        if confirmed:
            lines.append("\n## 已确认假设")
            for h in confirmed:
                lines.append(f"- {h['content']}")

        return "\n".join(lines)


AgentExecutorRegistry.register("prompt_experiment_v3", PromptExperimentV3Executor)
