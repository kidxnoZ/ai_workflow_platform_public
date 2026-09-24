"""Agent Loop 工具定义与执行器。

10 个工具供 Agent 自主选择调用：
1. transcribe_prompts — ASR 转写 prompt 音频
2. query_kb — 查询知识库
3. design_combos — 设计组合并执行合成
4. run_synthesis — 执行合成
5. request_user_review — 暂停等待用户评价
6. update_portfolio — 更新策略档案
7. write_experience — 写入 KB 经验
8. finish_experiment — 结束实验
9. update_model_knowledge — 追加模型新发现到 profile
10. distill_experience — 同步提炼 experience 为 factors/insights
"""
import json
import logging
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.config import OUTPUTS_DIR
from backend.tools import asr as asr_tool
from backend.tools import kb_tools
from backend.tools.audio_processing import (
    detect_total_duration, trim_silence, concat_audio, detect_silence_segments,
)

logger = logging.getLogger(__name__)

# ── Tool Definitions (Anthropic format) ──────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "transcribe_prompts",
        "description": "转写 prompt 音频，获取 ASR 文本。用于了解 prompt 内容特征。",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要转写的 prompt ID 列表（如 ['p0','p1']），不传则转写全部",
                },
            },
            "required": [],
        },
    },
    {
        "name": "query_kb",
        "description": "查询知识库（跨任务积累的精炼知识：模型档案/历史经验/因子规律/覆盖矩阵/历史策略）。建议先用 scope=all 获取完整视图，再用 scope=factors+keywords 定向读取具体因子全文。",
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "搜索关键词（scope=factors 时用于 grep 匹配因子内容）"},
                "scope": {
                    "type": "string",
                    "enum": ["profiles", "experiences", "factors", "factor_index", "insights", "coverage", "strategies", "all"],
                    "description": (
                        "搜索范围："
                        "profiles=模型档案, "
                        "experiences=原始实验记录, "
                        "factors=因子全文(需要 keywords grep), "
                        "factor_index=所有因子目录(key+rule+confidence 轻量视图，不需要 keywords), "
                        "insights=全局摘要, "
                        "coverage=覆盖矩阵, "
                        "strategies=历史任务最终策略档案, "
                        "all=全部(factors 部分返回 factor_index 目录而非全文)"
                    ),
                },
                "model_id": {
                    "type": "string",
                    "description": "查 profiles 时指定模型 ID（可选，不填则返回本任务所有模型）",
                },
                "experience_limit": {
                    "type": "integer",
                    "description": "返回最近几条 experience 文件（默认 3，最大 10）",
                },
            },
            "required": ["keywords", "scope"],
        },
    },
    {
        "name": "design_combos",
        "description": "设计实验组合方案。仅规划 base_texts 和 combos，不执行合成。设计完成后必须调用 run_synthesis 执行合成。combos × base_texts ≤ 30。",
        "input_schema": {
            "type": "object",
            "properties": {
                "base_texts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "本轮基准文本列表（2-5 条）",
                },
                "combos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "combo_id": {"type": "string"},
                            "type": {"type": "string", "enum": ["exploit", "diagnostic", "explore"]},
                            "model_id": {"type": "string"},
                            "prompt_ids": {"type": "array", "items": {"type": "string"}},
                            "text_strategy": {"type": "string"},
                            "text_variants": {
                                "type": "object",
                                "description": "模型特定的文本变体。格式：{model_id: text} 或 {model_id: 'text_t0|text_t1|...'}\n"
                                               "- 单条文本：{model_id: 'xxx'} → 所有 base_texts 均用此变体\n"
                                               "- 多条文本用 | 分隔：{model_id: 'variant_t0|variant_t1'} → 按 base_texts 顺序分别对应\n"
                                               "- 不填则使用对应的 base_text 原文",
                            },
                            "params": {"type": "object"},
                            "reasoning": {"type": "string"},
                            "control_var": {
                                "type": "string",
                                "enum": ["model", "prompt", "text", "params"],
                                "description": "diagnostic 类型专用。本次对照实验正在测试的变量。",
                            },
                            "baseline_combo": {
                                "type": "string",
                                "description": "diagnostic 类型专用。对照基准 combo_id（通常是某个 exploit combo，除 control_var 指定的变量外其余字段相同）。",
                            },
                        },
                        "required": ["combo_id", "type", "model_id", "prompt_ids"],
                    },
                    "description": "组合列表",
                },
            },
            "required": ["base_texts", "combos"],
        },
    },
    {
        "name": "run_synthesis",
        "description": "执行当前轮次的音频合成。必须在 design_combos 之后调用。耗时较长，合成完成后调用 request_user_review。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "request_user_review",
        "description": "暂停等待用户对合成结果进行评价排名。合成完成后必须调用此工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "round": {"type": "integer", "description": "当前轮次"},
                "message_to_user": {
                    "type": "string",
                    "description": "给用户的评价引导信息",
                },
            },
            "required": ["round"],
        },
    },
    {
        "name": "update_portfolio",
        "description": "更新策略档案。每次分析用户评价后调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "portfolio": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "strategy_id": {"type": "string"},
                            "model_id": {"type": "string"},
                            "prompt_ids": {"type": "array", "items": {"type": "string"}},
                            "text_strategy": {"type": "string"},
                            "params": {"type": "object"},
                            "profile": {"type": "string"},
                            "best_for": {"type": "array", "items": {"type": "string"}},
                            "avg_rank": {"type": "number"},
                            "vs_target_mode": {"type": "string"},
                        },
                        "required": ["strategy_id", "model_id", "profile"],
                    },
                },
                "convergence_assessment": {
                    "type": "string",
                    "description": "收敛评估（如：'top-3 连续 2 轮稳定' / '仍在探索'）",
                },
            },
            "required": ["portfolio"],
        },
    },
    {
        "name": "write_experience",
        "description": "写入知识库经验。每轮分析完评价后调用一次（本轮策略分析），实验结束前再调用一次（完整总结）。多次调用追加到同一文件。内容要求：围绕策略因果分析——哪个组合（模型+参数+prompt+文本策略）在什么条件下有效/失效，缺点标签归因到哪个因子，用户备注中的关键判断。不要复述排名数字，要写因果链。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scene_type": {"type": "string"},
                "content": {
                    "type": "string",
                    "description": "经验文件正文（markdown 格式）。围绕策略因果分析：组合效果、缺点归因、用户判断引用、跨轮变化。",
                },
                "control_evidence": {
                    "type": "array",
                    "description": "本轮 diagnostic 对照实验的归因证据。当本轮有 type=diagnostic 的 combo 时填写，每条对应一个 diagnostic combo 与其 baseline_combo 的分数对比。agent 根据评价结果自行填写。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "variable":       {"type": "string", "description": "正在测试的变量，如 prompt / model / text / params"},
                            "baseline_combo": {"type": "string", "description": "基准 combo_id"},
                            "baseline_value": {"type": "string", "description": "基准值的描述，如 'p0'"},
                            "baseline_score": {"type": "number", "description": "基准 combo 的得分"},
                            "variant_combo":  {"type": "string", "description": "对照 combo_id（diagnostic combo）"},
                            "variant_value":  {"type": "string", "description": "对照值的描述，如 'p1'"},
                            "variant_score":  {"type": "number", "description": "对照 combo 的得分"},
                            "held_constant":  {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "保持不变的变量，如 ['model=voxcpm', 'text=anchor_text']",
                            },
                            "conclusion": {"type": "string", "description": "本次对照的语义解读"},
                        },
                        "required": ["variable", "baseline_combo", "baseline_score",
                                     "variant_combo", "variant_score", "held_constant"],
                    },
                },
            },
            "required": ["scene_type", "content"],
        },
    },
    {
        "name": "finish_experiment",
        "description": "结束实验。当策略收敛或用户满意时调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "结束原因（如 'portfolio_converged' / 'user_satisfied' / 'max_rounds'）",
                },
                "summary": {"type": "string", "description": "实验总结"},
                "final_portfolio": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "最终策略档案（可选，不传则使用最后一次 update_portfolio 的结果）",
                },
            },
            "required": ["reason", "summary"],
        },
    },
    {
        "name": "update_model_knowledge",
        "description": "将本次实验对某模型的新认知追加到 KB 的 profile.md（和可选的 tag_tutorial.md）。在 write_experience 之后、finish_experiment 之前调用，每个有新发现的模型调用一次。",
        "input_schema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "模型 ID"},
                "profile_finding": {
                    "type": "string",
                    "description": "本次实验对该模型的新认知（适用场景、参数规律、已知限制等），markdown 格式",
                },
                "tag_finding": {
                    "type": "string",
                    "description": "（可选）若发现 tag/参数的新用法规律，追加到 tag_tutorial",
                },
            },
            "required": ["model_id", "profile_finding"],
        },
    },
    {
        "name": "distill_experience",
        "description": "将本任务最新写入的 experience 文件同步提炼为结构化 factors 和 insights。可选工具，适合多轮实验中途调用（在 write_experience 之后），让后续轮次能直接用 query_kb(scope=factors) 查到提炼好的因子。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ── Tool Handlers ────────────────────────────────────────────────────────────

def execute_tool(name: str, tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    """
    执行工具，返回 (result_json_str, should_pause)。
    should_pause=True 时 loop 应暂停等待用户操作。
    """
    handler = _HANDLERS.get(name)
    if not handler:
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False), False
    try:
        return handler(tool_input, task, task_input, db)
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False), False


# ── 1. transcribe_prompts ────────────────────────────────────────────────────

def _tool_transcribe_prompts(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    prompt_ids = tool_input.get("prompt_ids")
    prompts = task_input.get("prompts", [])

    results = []
    for i, p in enumerate(prompts):
        pid = f"p{i}"
        if prompt_ids and pid not in prompt_ids:
            continue
        try:
            text = asr_tool.transcribe(p["stored_path"])
        except Exception as e:
            text = f"[ASR ERROR: {e}]"
        p["asr_text"] = text
        result_entry = {"file_id": p.get("file_id", pid), "prompt_id": pid, "asr_text": text}
        # 返回 prompt 时长，供 Agent 判断是否满足模型最低要求（minimax/qwen-tts ≥10s）
        try:
            result_entry["duration"] = round(detect_total_duration(p["stored_path"]), 1)
        except Exception:
            pass
        if p.get("annotation"):
            result_entry["annotation"] = p["annotation"]
        results.append(result_entry)

    task.append_log(f"ASR 转写完成: {len(results)} 条")
    db.commit()
    return json.dumps({"transcriptions": results}, ensure_ascii=False), False


# ── 2. query_kb ──────────────────────────────────────────────────────────────

def _tool_query_kb(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    keywords = tool_input.get("keywords", "")
    scope = tool_input.get("scope", "all")
    model_id = tool_input.get("model_id")
    experience_limit = min(int(tool_input.get("experience_limit") or 3), 10)

    results = []

    if scope in ("profiles", "all"):
        target_models = [model_id] if model_id else task_input.get("model_ids", [])
        for mid in target_models:
            profile = kb_tools.read_model_profile(mid)
            if profile["profile"]:
                results.append({"type": "profile", "model": mid, "content": profile["profile"][:2000]})
            if profile["tag_tutorial"]:
                results.append({"type": "tag_tutorial", "model": mid, "content": profile["tag_tutorial"][:1500]})

    if scope in ("experiences", "all"):
        scene_type = task_input.get("scene_type", "")
        exp_paths = kb_tools.list_experiences(scene_type, limit=experience_limit)
        for ep in exp_paths:
            content = kb_tools.read_kb_file(ep)
            if not keywords or keywords.lower() in content.lower():
                results.append({"type": "experience", "file": ep, "content": content[:1500]})

    if scope == "factors" and keywords:
        # 精确 grep：关键词匹配因子全文
        factors = kb_tools.grep_kb(keywords, ["factors"])
        for f in factors[:5]:
            content = kb_tools.read_kb_file(f["file"])
            results.append({"type": "factor", "file": f["file"], "content": content[:800]})

    if scope in ("factor_index", "all"):
        # 轻量目录：key + rule + confidence，不含全文；按置信度排序（high → contested 排最后）
        index_result = kb_tools.list_factor_index()
        index = index_result["factors"]
        if index:
            _conf_order = {"high": 0, "medium": 1, "low": 2, "contested": 3}
            index.sort(key=lambda f: _conf_order.get(f.get("confidence", "low"), 2))
            # 给每条因子加可读标注，帮助 Agent 理解置信度含义
            for f in index:
                c = f.get("confidence", "low")
                _hints = {"high": "≥2 causal，较可信", "medium": "有证据但非强因果", "low": "仅1条证据，仅供参考", "contested": "有反例，争议中"}
                f["confidence_hint"] = _hints.get(c, "")
            results.append({"type": "factor_index", "content": index})

    if scope in ("insights", "all"):
        insight_files = kb_tools.glob_kb("*.md", "insights")
        for fp in insight_files:
            content = kb_tools.read_kb_file(fp)
            if not keywords or keywords.lower() in content.lower():
                results.append({"type": "insight", "file": fp, "content": content[:2000]})

    if scope in ("coverage", "all"):
        model_ids = task_input.get("model_ids", [])
        scene_type = task_input.get("scene_type", "")
        coverage = kb_tools.check_coverage(scene_type, model_ids)
        results.append({"type": "coverage", "content": coverage["summary"]})

    if scope in ("strategies", "all"):
        scene_type = task_input.get("scene_type", "")
        strategy_files = kb_tools.glob_kb("**/*.json", f"strategies/{scene_type}" if scene_type else "strategies")
        for fp in strategy_files[-5:]:
            content = kb_tools.read_kb_file(fp)
            if content:
                results.append({"type": "strategy", "file": fp, "content": content[:2000]})

    return json.dumps({"results": results}, ensure_ascii=False), False


# ── 3. design_combos ─────────────────────────────────────────────────────────

def _tool_design_combos(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    from backend.models.task import Task as TaskModel, ModelRegistry
    from backend.executors.base import ExecutorFactory
    import backend.executors.subprocess_exec  # noqa
    import backend.executors.sglang_exec      # noqa
    import backend.executors.api_exec         # noqa

    base_texts = tool_input.get("base_texts")
    combos = tool_input.get("combos")

    if not base_texts or not combos:
        return json.dumps({"error": "缺少必填参数 base_texts（文本列表）和 combos（组合列表），请重新调用并提供这两个参数"}), False

    # 约束
    total = len(combos) * len(base_texts)
    if total > 30:
        combos = combos[:30 // max(len(base_texts), 1)]
        total = len(combos) * len(base_texts)
        task.append_log(f"约束裁剪: combo 数调整为 {len(combos)}，总合成数 {total}")

    # 更新 round，存储方案
    task_input["round"] = task_input.get("round", 0) + 1
    round_num = task_input["round"]
    task_input["current_round_plan"] = {"round": round_num, "base_texts": base_texts, "combos": combos}
    task.input = json.dumps(task_input, ensure_ascii=False)
    db.commit()

    return json.dumps({
        "round": round_num,
        "combos_count": len(combos),
        "base_texts_count": len(base_texts),
        "total_to_synthesize": total,
        "message": "方案已确定，请调用 run_synthesis 执行合成",
    }, ensure_ascii=False), False


# ── 3b. run_synthesis ────────────────────────────────────────────────────────

def _tool_run_synthesis(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    from backend.models.task import Task as TaskModel, ModelRegistry
    from backend.executors.base import ExecutorFactory
    import backend.executors.subprocess_exec  # noqa
    import backend.executors.sglang_exec      # noqa
    import backend.executors.api_exec         # noqa

    plan = task_input.get("current_round_plan")
    if not plan:
        return json.dumps({"error": "未找到当前轮次方案，请先调用 design_combos"}), False

    base_texts = plan["base_texts"]
    combos = plan["combos"]
    round_num = plan["round"]
    total = len(combos) * len(base_texts)

    synthesis_results = []
    done = 0
    task_input["synthesis_progress"] = {"done": 0, "total": total}
    task.input = json.dumps(task_input, ensure_ascii=False)
    db.commit()

    task_output_dir = OUTPUTS_DIR / task.id
    task_output_dir.mkdir(parents=True, exist_ok=True)

    # 按 model_id 分组
    model_groups: OrderedDict[str, list] = OrderedDict()
    for combo in combos:
        mid = combo.get("model_id", "")
        model_groups.setdefault(mid, []).append(combo)

    for model_id, group_combos in model_groups.items():
        model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
        if not model or model.status != "active":
            for combo in group_combos:
                for ti, text in enumerate(base_texts):
                    synthesis_results.append({
                        "combo_id": combo["combo_id"], "round": round_num,
                        "text_idx": ti, "text": text, "model_id": model_id,
                        "status": "failed", "error_reason": f"模型 {model_id} 不可用",
                        "audio_url": None, "audio_label": f"t{ti}_{combo['combo_id']}",
                        "prompt_ids": combo.get("prompt_ids", []), "sub_task_id": None,
                    })
                    done += 1
            continue

        # 构建同模型的所有 items
        batch_items = []
        batch_meta = []
        for combo in group_combos:
            prompt_ids = combo.get("prompt_ids", [])
            combo_params = combo.get("params", {})
            processed_prompt = _process_prompt_audio(task_input, prompt_ids, model_id)

            prompts = task_input.get("prompts", [])
            source_text = ""
            for pid in prompt_ids:
                idx = int(pid.replace("p", ""))
                if idx < len(prompts):
                    source_text += prompts[idx].get("asr_text", "") + " "
            source_text = source_text.strip()

            scene_form = task_input.get("scene_form", {})
            context_prompt = scene_form.get("context_prompt", "")

            for text_idx, base_text in enumerate(base_texts):
                raw_variants = combo.get("text_variants", {})
                if isinstance(raw_variants, str):
                    text_variant = raw_variants
                elif isinstance(raw_variants, list):
                    text_variant = raw_variants[text_idx] if text_idx < len(raw_variants) else base_text
                elif isinstance(raw_variants, dict):
                    text_variant = raw_variants.get(model_id, raw_variants.get(str(text_idx), base_text))
                else:
                    text_variant = base_text
                if not isinstance(text_variant, str):
                    text_variant = str(text_variant) if text_variant else base_text
                # 支持 agent 用 | 分隔多条文本变体，按 text_idx 取对应条
                if isinstance(text_variant, str) and '|' in text_variant:
                    parts = [p.strip() for p in text_variant.split('|')]
                    if text_idx < len(parts) and parts[text_idx]:
                        text_variant = parts[text_idx]
                    elif parts[0]:
                        text_variant = parts[0]
                combo_id = combo["combo_id"]
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
                    "text_idx": text_idx,
                    "text": text_variant,
                    "model_id": model_id,
                    "prompt_ids": prompt_ids,
                    "audio_label": audio_label,
                })

        # 创建子任务
        sub_input = {
            "model_id": model_id,
            "items": batch_items,
            "parent_task_id": task.id,
            "parent_synth_offset": done,   # 本批开始时全局已完成条数，供逐条更新用
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

        task_input["current_sub_task_id"] = sub_task.id
        task_input["current_synth_model"] = model_id
        task.input = json.dumps(task_input, ensure_ascii=False)
        db.commit()

        try:
            executor = ExecutorFactory.get(model.model_type)
            result = executor.execute(sub_task, model, db)
            sub_task.status = "success"
            sub_task.result = json.dumps(result.to_dict(), ensure_ascii=False)
            sub_task.finished_at = datetime.utcnow()
            db.commit()

            for meta in batch_meta:
                synthesis_results.append({
                    "combo_id": meta["combo_id"], "round": round_num,
                    "text_idx": meta["text_idx"], "text": meta["text"],
                    "model_id": meta["model_id"], "prompt_ids": meta["prompt_ids"],
                    "audio_url": f"/storage/outputs/{sub_task.id}/{meta['audio_label']}.wav",
                    "audio_label": meta["audio_label"],
                    "sub_task_id": sub_task.id,
                    "status": "success", "error_reason": None,
                })
                done += 1

            task.append_log(f"模型 {model_id}: {result.ok} 成功 / {result.fail} 失败")
            db.commit()
        except Exception as e:
            sub_task.status = "failed"
            sub_task.error = str(e)
            sub_task.finished_at = datetime.utcnow()
            db.commit()
            for meta in batch_meta:
                synthesis_results.append({
                    "combo_id": meta["combo_id"], "round": round_num,
                    "text_idx": meta["text_idx"], "text": meta["text"],
                    "model_id": meta["model_id"], "prompt_ids": meta["prompt_ids"],
                    "audio_url": None, "audio_label": meta["audio_label"],
                    "sub_task_id": sub_task.id,
                    "status": "failed", "error_reason": str(e),
                })
                done += 1
            task.append_log(f"模型 {model_id} 批量合成失败: {e}", "error")
            db.commit()

        task_input["synthesis_progress"] = {"done": done, "total": total}
        task.input = json.dumps(task_input, ensure_ascii=False)
        db.commit()

    task_input["synthesis_results"] = synthesis_results
    ok_count = sum(1 for r in synthesis_results if r["status"] == "success")

    return json.dumps({
        "round": round_num,
        "total": len(synthesis_results),
        "ok": ok_count,
        "fail": len(synthesis_results) - ok_count,
    }, ensure_ascii=False), False


# ── 4. request_user_review ───────────────────────────────────────────────────

def _tool_request_user_review(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    round_num = tool_input.get("round", task_input.get("round", 1))
    message = tool_input.get("message_to_user", "请对本轮合成结果进行评价")
    task_input["_review_message"] = message
    task.append_log(f"第 {round_num} 轮合成完成，等待用户评价")
    db.commit()
    return json.dumps({"status": "paused_for_review", "round": round_num}), True


# ── 5. update_portfolio ──────────────────────────────────────────────────────

def _tool_update_portfolio(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    portfolio = tool_input["portfolio"]
    convergence = tool_input.get("convergence_assessment", "")
    task_input["portfolio"] = portfolio
    task_input["convergence_assessment"] = convergence
    task.append_log(f"策略档案更新: {len(portfolio)} 个策略")
    db.commit()
    return json.dumps({"ok": True, "portfolio_size": len(portfolio)}), False


# ── 6. write_experience ──────────────────────────────────────────────────────

def _tool_write_experience(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    scene_type = tool_input["scene_type"]
    content = tool_input["content"]
    control_evidence = tool_input.get("control_evidence", [])

    # 将 control_evidence 追加到 content 末尾，渲染为 markdown 段落
    if control_evidence:
        ce_lines = ["\n\n## 对照实验归因证据"]
        for ce in control_evidence:
            variable = ce.get("variable", "")
            b_combo  = ce.get("baseline_combo", "")
            b_value  = ce.get("baseline_value", "")
            b_score  = ce.get("baseline_score", "")
            v_combo  = ce.get("variant_combo", "")
            v_value  = ce.get("variant_value", "")
            v_score  = ce.get("variant_score", "")
            held     = ", ".join(ce.get("held_constant", []))
            concl    = ce.get("conclusion", "")
            delta    = round(v_score - b_score, 2) if isinstance(v_score, (int, float)) and isinstance(b_score, (int, float)) else "?"
            sign     = "+" if isinstance(delta, (int, float)) and delta >= 0 else ""
            ce_lines.append(
                f"\n### 变量: {variable}\n"
                f"- 基准 ({b_combo}, {b_value}): {b_score} 分\n"
                f"- 对照 ({v_combo}, {v_value}): {v_score} 分　delta={sign}{delta}\n"
                f"- 固定变量: {held}\n"
                f"- 结论: {concl}"
            )
        content = content + "\n".join(ce_lines)

    path = kb_tools.write_experience_file(task.id, scene_type, content)
    task_input["experience_file_path"] = path
    task.append_log(f"经验写入: {path}")
    db.commit()

    # 更新覆盖矩阵
    portfolio = task_input.get("portfolio", [])
    for strategy in portfolio:
        mid = strategy.get("model_id", "")
        avg_rank = strategy.get("avg_rank", 0)
        if mid:
            kb_tools.update_coverage_matrix(scene_type, mid, avg_rank, task_input.get("round", 1))

    return json.dumps({"path": path}), False


# ── 7. finish_experiment ─────────────────────────────────────────────────────

def _tool_finish_experiment(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    from backend.models.task import Task as TaskModel

    reason = tool_input["reason"]
    summary = tool_input["summary"]
    final_portfolio = tool_input.get("final_portfolio", task_input.get("portfolio", []))

    task_input["phase"] = "success"
    task_input["portfolio"] = final_portfolio
    task.result = json.dumps({
        "reason": reason,
        "portfolio": final_portfolio,
        "summary": summary,
        "total_rounds": task_input.get("round", 0),
    }, ensure_ascii=False)
    task.status = "success"
    task.finished_at = datetime.utcnow()
    task.input = json.dumps(task_input, ensure_ascii=False)
    task.append_log(f"实验完成: {reason}")
    db.commit()

    # 将最终策略档案持久化到 KB
    if final_portfolio:
        scene_type = task_input.get("scene_type", "unknown")
        strategy_path = kb_tools.write_strategy_file(task.id, scene_type, final_portfolio)
        task.append_log(f"策略档案已写入 KB: {strategy_path}")
        db.commit()

    # 触发异步 KB 聚合
    agg_task = TaskModel(
        type="kb_aggregation",
        status="pending",
        input=json.dumps({
            "trigger_task_id": task.id,
            "scene_type": task_input.get("scene_type", ""),
            "experience_file": task_input.get("experience_file_path", ""),
        }, ensure_ascii=False),
    )
    db.add(agg_task)
    db.commit()
    task.append_log(f"KB 聚合任务已创建: {agg_task.id}")
    db.commit()

    # emit timeline 事件，让前端可见后台聚合已触发
    events = task_input.setdefault("agent_events", [])
    events.append({
        "type": "system_event",
        "content": "已提交知识库聚合任务，后台运行中",
        "ts": datetime.utcnow().isoformat(),
    })
    if len(events) > 200:
        events[:] = events[-200:]
    task.input = json.dumps(task_input, ensure_ascii=False)
    db.commit()

    return json.dumps({"ok": True}), False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _process_prompt_audio(task_input: dict, prompt_ids: list[str], model_id: str = "") -> str:
    """Process prompt audio with model-aware constraints.

    Rules:
    - Single prompt: trim silence, enforce model max duration
    - Multiple prompts each < 10s and total < 30s: concatenate
    - Single prompt > 20s with long silence: split and use best segment
    - cosyvoice3: hard limit 30s, reject longer prompts
    """
    MODEL_MAX_PROMPT_DURATION = {
        "cosyvoice3": 30.0,
    }
    max_dur = MODEL_MAX_PROMPT_DURATION.get(model_id, 60.0)

    prompts = task_input.get("prompts", [])
    paths = []
    for pid in prompt_ids:
        idx = int(pid.replace("p", ""))
        if idx < len(prompts):
            paths.append(prompts[idx]["stored_path"])

    if not paths:
        return ""

    durations = [(p, detect_total_duration(p)) for p in paths]

    if len(paths) == 1:
        path, dur = durations[0]
        trimmed = trim_silence(path)
        trimmed_dur = detect_total_duration(trimmed)
        if trimmed_dur <= max_dur:
            return trimmed
        # Too long — try to find a good segment by splitting on silence
        segments = detect_silence_segments(path, threshold_db=-40.0, min_silence_ms=3000)
        if segments:
            # Use the longest speech segment before the first long pause
            first_pause_start = segments[0]["start_ms"] / 1000.0
            if first_pause_start >= 5.0 and first_pause_start <= max_dur:
                return trimmed
        # Last resort: just return trimmed and let the model handle the error
        return trimmed

    # Multiple prompts: decide concat vs pick-best
    all_short = all(d < 10.0 for _, d in durations)
    total = sum(d for _, d in durations)

    if all_short and total < max_dur:
        return concat_audio(paths, gap_ms=100)

    # Not all short or would exceed limit: use the single longest prompt that fits
    valid = [(p, d) for p, d in durations if d <= max_dur]
    if valid:
        best = max(valid, key=lambda x: x[1])
        return trim_silence(best[0])

    # All exceed limit: use shortest (closest to limit after trim)
    shortest = min(durations, key=lambda x: x[1])
    return trim_silence(shortest[0])


# ── 9. update_model_knowledge ────────────────────────────────────────────────

def _tool_update_model_knowledge(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    model_id = tool_input.get("model_id", "").strip()
    profile_finding = tool_input.get("profile_finding", "").strip()
    tag_finding = tool_input.get("tag_finding", "").strip()

    if not model_id or not profile_finding:
        return json.dumps({"error": "缺少必填参数 model_id 或 profile_finding"}), False

    result = kb_tools.append_model_profile(model_id, profile_finding, tag_finding)
    task.append_log(f"模型知识更新: {model_id} → {result.get('profile', '')}")
    db.commit()
    return json.dumps({"ok": True, "written": result}, ensure_ascii=False), False


# ── 10. distill_experience ───────────────────────────────────────────────────

def _tool_distill_experience(tool_input: dict, task, task_input: dict, db: Session) -> tuple[str, bool]:
    from backend.executors.kb_aggregation import KBAggregationExecutor
    from backend.tools.kb_tools import KB_ROOT
    from datetime import date

    exp_path = task_input.get("experience_file_path", "")
    if not exp_path:
        return json.dumps({"error": "尚未写入 experience 文件，请先调用 write_experience"}), False

    exp_content = kb_tools.read_kb_file(exp_path)
    if not exp_content:
        return json.dumps({"error": f"experience 文件为空或不存在: {exp_path}"}), False

    experience_texts = [f"### 文件: {exp_path}\n\n{exp_content}"]

    # 读取全量 factor_index + 相关全文（与 kb_aggregation._run_aggregation 一致）
    agg = KBAggregationExecutor()
    index_result = kb_tools.list_factor_index()
    factor_index = index_result["factors"]
    known_dimensions = index_result["known_dimensions"]
    exp_subjects, _ = agg._extract_subjects_and_scenes([exp_path], experience_texts)
    relevant_factor_texts = []
    for entry in factor_index:
        subj = entry.get("factor_subject", "").lower()
        if subj:
            hit = any(s in subj for s in exp_subjects) or subj in ("prompt", "text")
        else:
            hit = any(s in entry.get("factor_key", "").lower() for s in exp_subjects)
        if hit:
            full = kb_tools.read_kb_file(entry["file"])
            if full:
                relevant_factor_texts.append(f"### {entry['file']}\n\n{full}")

    agg = KBAggregationExecutor()
    user_msg = agg._build_user_message(experience_texts, factor_index, known_dimensions, relevant_factor_texts)
    task.append_log("distill_experience: 调用 LLM 提炼因子…")
    db.commit()

    result = agg._call_llm(user_msg)
    factors = result.get("factors", [])
    insights = result.get("insights", {})

    agg._write_factors(factors, factor_index)
    agg._write_insights(insights)

    # 更新 _last_aggregated.md，防止后台聚合重复处理
    last_agg_path = KB_ROOT / "_last_aggregated.md"
    processed: set[str] = set()
    if last_agg_path.exists():
        for line in last_agg_path.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if line.startswith("- "):
                processed.add(line[2:].strip())
    processed.add(exp_path)
    agg_content = f"# KB 聚合记录\n\nlast_aggregated: {date.today().isoformat()}\nprocessed_files:\n"
    for fp in sorted(processed):
        agg_content += f"- {fp}\n"
    last_agg_path.write_text(agg_content, encoding="utf-8")

    task.append_log(f"distill_experience 完成: {len(factors)} 个因子，insights 已更新")
    db.commit()
    return json.dumps({
        "factors_written": len(factors),
        "insights_updated": bool(insights),
        "factor_keys": [
            f"{f.get('factor_subject','')}-{f.get('factor_dimension','')}-{f.get('factor_scene','')}"
            for f in factors
        ],
    }, ensure_ascii=False), False


# ── Handler Registry ─────────────────────────────────────────────────────────

_HANDLERS = {
    "transcribe_prompts": _tool_transcribe_prompts,
    "query_kb": _tool_query_kb,
    "design_combos": _tool_design_combos,
    "run_synthesis": _tool_run_synthesis,
    "request_user_review": _tool_request_user_review,
    "update_portfolio": _tool_update_portfolio,
    "write_experience": _tool_write_experience,
    "finish_experiment": _tool_finish_experiment,
    "update_model_knowledge": _tool_update_model_knowledge,
    "distill_experience": _tool_distill_experience,
}
