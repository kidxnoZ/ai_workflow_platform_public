"""KB 聚合 Agent 执行器。

异步后台任务：从增量 experience 文件中提炼因子规律，更新 insights 全局摘要。
触发时机：每次实验结束（finish_experiment）后自动创建。

流程：
1. 读 _last_aggregated.md → 获取已处理文件列表
2. 扫描 experiences/ → 过滤出增量文件
3. 读增量 experience 全文 + 已有 factors 全文
4. 调 LLM（单次 structured output）→ 返回 factors + insights
5. 写入/更新 kb/factors/ 和 kb/insights/
6. 更新 _last_aggregated.md
"""
import json
import logging
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.config import (
    ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL, ANTHROPIC_MODEL,
)
from backend.executors.agent_base import AgentBaseExecutor, AgentExecutorRegistry
from backend.tools import kb_tools
from backend.tools.kb_tools import KB_ROOT

logger = logging.getLogger(__name__)

AGGREGATION_SYSTEM_PROMPT = """# 角色
你是知识库聚合 Agent。你的任务是从语音克隆实验的原始经验记录中提炼可复用的结构化知识。

# 输入
你会收到：
1. 增量 experience 文件（本次新增的实验记录）
2. 已有 Factor 目录（全量轻量索引：key + rule + confidence，不含全文）
3. 相关 Factor 全文（按模型名筛选，仅包含与本次 experience 相关的因子）
4. 已有 Dimension 列表（所有历史因子用过的维度词）

# 核心设计原则：场景是语义上下文，不是知识结构

场景（脱口秀/有声书/广播新闻等）是 agent 理解任务背景的语义特征，不是切割知识库的结构维度。
同一模型的跨轮稳定性在脱口秀和相声中本质相同；短句对所有模型更友好也与场景无关。
将场景写入 factor key 会导致跨场景数据无法复用，知识碎片化。

正确做法：
- factor key = {subject}-{dimension}，不含场景
- 场景上下文写入 evidence 文本（如 "[2026-07-21, 脱口秀] higgs rank1..."）
- 如果规律确实有场景边界，写入 applicable_conditions / not_applicable

# 输出要求
通过 structured_output 工具返回以下结构：

## factors（原子因子规律）
每个 factor 是一条可独立验证的原子结论，分三类：
- **model_factors**: 某模型的表现规律（跨场景通用，除非有明确场景边界）
- **prompt_factors**: prompt 音频特征对结果的影响规律
  - experience 中记录的 prompt 标注字段（recording_env / speaking_pace / style / paralanguage）是归因的主要依据
  - factor_dimension 优先使用标注字段名：recording_env_effect / speaking_pace_effect / style_match / paralanguage_effect
  - rule 示例："录音棚近场录制的 prompt 相似度高于安静室内/线上会议录制"
  - evidence text 中注明 prompt 的具体标注值，如 "[p0, 录音棚近场, 偏快] vs [p1, 线上会议, 正常]"
- **text_factors**: 文本策略对结果的影响规律

每个 factor 包含：
- factor_subject: 主体，通常是模型 ID（higgs/cosyvoice3/...）或 'prompt'/'text'
- factor_dimension: 维度词，**优先从 [已有 Dimension 列表] 中选择最匹配的**；若无合适的，可自行命名（中文或英文，保持原子性，不含 subject 信息）
- factor_type: model_factors / prompt_factors / text_factors
- rule: 一句话结论（除非规律本身是场景特定的，否则不在 rule 里提场景）
- evidence: 证据列表，每条为对象 {"text": "[日期, 场景] 任务名: 描述", "type": "causal"|"correlational"}
  - causal: 来自 control_evidence（单变量对照实验，可做因果归因）
  - correlational: 多变量同时变化，仅相关性参考
  - 注意：evidence text 里保留场景信息作为上下文
- applicable_conditions: 适用条件列表（若规律有场景边界，在此注明）
- not_applicable: 已知不适用的条件
- confidence: 依据下方规则计算

confidence 计算规则：
- low: 1 条证据（不论类型）
- medium: ≥2 条 correlational，或 1 条 causal
- high: ≥2 条 causal 且无反例
- contested: 存在反例（不论类型）

## insights（全局摘要）
从所有 factors 中生成的横向对比视图：
- model_comparison: 各模型强弱对比摘要（基于所有 model_factors）
- text_strategy_summary: 文本策略经验摘要（基于所有 text_factors）
- prompt_pairing_summary: prompt 特征与模型匹配规律（基于 prompt_factors + model_factors 交叉）

每个摘要字段是一段 markdown 文本（200-500 字），适合 agent 一次性阅读理解。

# 规则
1. **只基于证据**：每个 factor 必须有具体证据支撑，不推测原因
2. **优先更新已有 factor**：在 [已有 Factor 目录] 中查找 subject+dimension 匹配的条目（不含场景），找到则追加 evidence 并重新计算 confidence，**不新建**
3. **dimension 复用优先**：从 [已有 Dimension 列表] 选词；列表中确实没有合适的才新建维度词
4. **发现矛盾时**：将 confidence 降为 contested，在 not_applicable 中注明反例条件
5. **不删除旧结论**：即使反例出现也保留原 factor，标注 contested
6. **insights 从 factors 生成**：insights 是 factors 的可读性视图，不包含 factors 中没有的信息
7. **保持原子性**：每个 factor 只描述一个变量的影响，不混合多因子"""

AGGREGATION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "factors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "factor_subject":    {"type": "string"},
                    "factor_dimension":  {"type": "string"},
                    "factor_type": {
                        "type": "string",
                        "enum": ["model_factors", "prompt_factors", "text_factors"],
                    },
                    "rule": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "type": {"type": "string", "enum": ["causal", "correlational"]},
                            },
                            "required": ["text", "type"],
                        },
                    },
                    "applicable_conditions": {"type": "array", "items": {"type": "string"}},
                    "not_applicable":        {"type": "array", "items": {"type": "string"}},
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "contested"],
                    },
                },
                "required": [
                    "factor_subject", "factor_dimension",
                    "factor_type", "rule", "evidence", "confidence",
                ],
            },
        },
        "insights": {
            "type": "object",
            "properties": {
                "model_comparison":      {"type": "string"},
                "text_strategy_summary": {"type": "string"},
                "prompt_pairing_summary":{"type": "string"},
            },
            "required": ["model_comparison", "text_strategy_summary", "prompt_pairing_summary"],
        },
    },
    "required": ["factors", "insights"],
}


class KBAggregationExecutor(AgentBaseExecutor):

    def execute(self, task, db: Session) -> None:
        input_data = json.loads(task.input or "{}")
        task.status = "running"
        task.append_log("KB 聚合开始")
        db.commit()

        try:
            self._run_aggregation(task, input_data, db)
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            task.finished_at = datetime.utcnow()
            task.append_log(f"聚合失败: {e}", "error")
            db.commit()
            logger.exception("KB aggregation failed")

    def _run_aggregation(self, task, input_data: dict, db: Session):
        # Step 1: 读取已处理文件列表
        last_agg_path = KB_ROOT / "_last_aggregated.md"
        processed_files = set()
        if last_agg_path.exists():
            content = last_agg_path.read_text(encoding="utf-8")
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("- "):
                    processed_files.add(line[2:].strip())

        # Step 2: 扫描增量 experience 文件
        all_experiences = kb_tools.glob_kb("**/*.md", "experiences")
        new_experiences = [f for f in all_experiences if f not in processed_files]

        if not new_experiences:
            task.status = "success"
            task.result = json.dumps({"message": "无增量文件，跳过聚合"}, ensure_ascii=False)
            task.finished_at = datetime.utcnow()
            task.append_log("无增量 experience 文件")
            db.commit()
            return

        # Step 3: 读取增量 experience 全文
        experience_texts = []
        for fp in new_experiences:
            content = kb_tools.read_kb_file(fp)
            if content:
                experience_texts.append(f"### 文件: {fp}\n\n{content}")

        # 读取全量 factor_index（轻量目录），再按 subject 筛选相关全文
        index_result = kb_tools.list_factor_index()
        factor_index = index_result["factors"]
        known_dimensions = index_result["known_dimensions"]

        # 从 experience 文本提取模型名，按 subject 精准筛选相关因子全文
        exp_subjects, _ = self._extract_subjects_and_scenes(new_experiences, experience_texts)
        relevant_factor_texts = []
        for entry in factor_index:
            subj = entry.get("factor_subject", "").lower()
            if subj:
                hit = any(s in subj for s in exp_subjects) or subj in ("prompt", "text")
            else:
                # 旧格式无 subject 字段，回退到 factor_key 匹配
                hit = any(s in entry.get("factor_key", "").lower() for s in exp_subjects)
            if hit:
                full = kb_tools.read_kb_file(entry["file"])
                if full:
                    relevant_factor_texts.append(f"### {entry['file']}\n\n{full}")

        task.append_log(
            f"增量文件: {len(new_experiences)}, 已有因子: {len(factor_index)}, "
            f"相关全文: {len(relevant_factor_texts)}"
        )
        db.commit()

        # Step 4: 调 LLM
        user_msg = self._build_user_message(experience_texts, factor_index, known_dimensions, relevant_factor_texts)
        result = self._call_llm(user_msg)

        # Step 5: 写入文件
        factors = result.get("factors", [])
        insights = result.get("insights", {})

        self._write_factors(factors, factor_index)
        self._write_insights(insights)

        task.append_log(f"写入 {len(factors)} 个因子, insights 已更新")
        db.commit()

        # Step 6: 更新 _last_aggregated.md
        all_processed = processed_files | set(new_experiences)
        agg_content = f"# KB 聚合记录\n\nlast_aggregated: {date.today().isoformat()}\nprocessed_files:\n"
        for fp in sorted(all_processed):
            agg_content += f"- {fp}\n"
        last_agg_path.write_text(agg_content, encoding="utf-8")

        task.status = "success"
        task.result = json.dumps({
            "new_experiences": len(new_experiences),
            "factors_written": len(factors),
            "insights_updated": True,
        }, ensure_ascii=False)
        task.finished_at = datetime.utcnow()
        task.append_log("KB 聚合完成")
        db.commit()

    def _build_user_message(
        self,
        experience_texts: list[str],
        factor_index: list[dict],
        known_dimensions: list[str],
        relevant_factor_texts: list[str],
    ) -> str:
        parts = []

        parts.append("## 增量 Experience 文件\n")
        parts.append("\n\n---\n\n".join(experience_texts[:20]))

        parts.append("\n\n## 已有 Factor 目录（全量轻量索引）\n")
        if factor_index:
            for e in factor_index:
                key = e.get("factor_key", "")
                conf = e.get("confidence", "")
                rule = e.get("rule", "")
                parts.append(f"- `{key}` [{conf}] {rule}")
        else:
            parts.append("（无，首次聚合）")

        if relevant_factor_texts:
            parts.append("\n\n## 相关 Factor 全文（按关键字筛选，需要时追加 evidence）\n")
            parts.append("\n\n---\n\n".join(relevant_factor_texts))

        if known_dimensions:
            parts.append(f"\n\n## 已有 Dimension 列表\n{', '.join(known_dimensions)}")
        else:
            parts.append("\n\n## 已有 Dimension 列表\n（无，首次聚合，请自行命名维度）")

        parts.append("\n\n---\n\n请分析以上实验记录，提炼因子规律并生成全局摘要。")
        return "\n".join(parts)

    def _extract_subjects_and_scenes(
        self, experience_paths: list[str], experience_texts: list[str]
    ) -> tuple[set, set]:
        """从 experience 文件路径和文本中提取模型名（subjects）和场景词（scenes）。
        用于按 factor 结构化字段筛选相关全文，比关键字匹配精准得多。
        """
        import re
        known_models = [
            "higgs", "cosyvoice", "cosy-voice", "fish-audio", "fishaudio",
            "voxcpm", "voxcpm2", "mimo", "qwen", "elevenlabs", "eleven",
            "index-tts", "indextts", "moss",
        ]
        text_all = " ".join(experience_texts).lower()
        # 从路径提取场景（experiences/{scene_type}/{file}.md 的第二段）
        scenes: set[str] = set()
        for fp in experience_paths:
            parts = fp.replace("\\", "/").split("/")
            # experiences / scene_parts / file.md
            if len(parts) >= 3:
                # scene 可能包含多级目录，取中间所有段
                scene_parts = parts[1:-1]
                for sp in scene_parts:
                    scenes.add(sp.lower().strip())
        # 从文本中提取出现的模型名
        subjects: set[str] = set()
        for m in known_models:
            if m in text_all:
                subjects.add(m)
        return subjects, scenes

    def _call_llm(self, user_msg: str) -> dict:
        from anthropic import Anthropic

        kwargs = {}
        if ANTHROPIC_BASE_URL:
            kwargs["base_url"] = ANTHROPIC_BASE_URL
        if ANTHROPIC_AUTH_TOKEN:
            kwargs["api_key"] = ANTHROPIC_AUTH_TOKEN
        elif ANTHROPIC_API_KEY:
            kwargs["api_key"] = ANTHROPIC_API_KEY
        client = Anthropic(**kwargs)

        call_kwargs = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 16000,
            "system": AGGREGATION_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_msg}],
            "tools": [{
                "name": "structured_output",
                "description": "输出结构化聚合结果",
                "input_schema": AGGREGATION_OUTPUT_SCHEMA,
            }],
            # tool_choice=auto: thinking 模式不支持强制指定工具名
            "tool_choice": {"type": "auto"},
            # 禁用 thinking：聚合是归纳任务，不是创造性推理；
            # DeepSeek V4 Pro thinking 模式会耗尽 output token 配额导致 tool_use input 为空
            "thinking": {"type": "disabled"},
            "timeout": 120,
        }

        response = client.messages.create(**call_kwargs)

        # 优先从 tool_use block 取结构化结果
        for block in response.content:
            if block.type == "tool_use":
                return block.input

        # fallback：从文本中解析 JSON（thinking 模式下模型可能直接输出 JSON）
        for block in response.content:
            if block.type == "text" and block.text:
                text = block.text.strip()
                # 尝试提取 ```json ... ``` 块
                import re
                m = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group(1))
                    except json.JSONDecodeError:
                        pass
                # 尝试整段作为 JSON
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass

        raise ValueError("LLM 未返回 structured_output，也未找到可解析的 JSON 文本")

    def _write_factors(self, factors: list[dict], factor_index: list[dict] = None):
        # 构建 (subject, dimension) → index entry 查找表，场景不进 key
        lookup: dict[tuple, dict] = {}
        if factor_index:
            for e in factor_index:
                subj = e.get("factor_subject", "")
                dim  = e.get("factor_dimension", "")
                if subj and dim:
                    lookup[(subj, dim)] = e

        for factor in factors:
            subject     = factor.get("factor_subject", "")
            dimension   = factor.get("factor_dimension", "")
            factor_type = factor.get("factor_type", "model_factors")

            # key = subject-dimension（不含场景）
            key = f"{subject}-{dimension}".replace("/", "_").replace(" ", "_")

            existing_entry = lookup.get((subject, dimension))
            if existing_entry:
                existing_content = kb_tools.read_kb_file(existing_entry["file"])
                merged = self._merge_factor(existing_content, factor)
                fpath = KB_ROOT / existing_entry["file"]
            else:
                merged = factor
                dir_path = KB_ROOT / "factors" / factor_type
                dir_path.mkdir(parents=True, exist_ok=True)
                fpath = dir_path / f"{key}.md"

            fpath.write_text(self._render_factor(merged, key), encoding="utf-8")

    def _merge_factor(self, existing_content: str, new_factor: dict) -> dict:
        """将 new_factor 的 evidence 合并到从 existing_content 解析出的 factor 中，重新计算 confidence。"""
        # 解析已有文件的 evidence 列表（兼容旧格式字符串和新格式对象）
        existing_evidences = []
        in_evidence_section = False
        for line in existing_content.split("\n"):
            stripped = line.strip()
            if stripped == "## 证据":
                in_evidence_section = True
                continue
            if stripped.startswith("## ") and in_evidence_section:
                break
            if in_evidence_section and stripped.startswith("- ") and stripped != "- （无）":
                existing_evidences.append({"text": stripped[2:], "type": "correlational"})

        new_evidences = new_factor.get("evidence", [])
        # 标准化新 evidence（兼容字符串格式）
        normalized_new = []
        for e in new_evidences:
            if isinstance(e, str):
                normalized_new.append({"text": e, "type": "correlational"})
            elif isinstance(e, dict):
                normalized_new.append(e)

        # 合并，按 text 去重
        seen_texts = {e["text"] for e in existing_evidences}
        for e in normalized_new:
            if e["text"] not in seen_texts:
                existing_evidences.append(e)
                seen_texts.add(e["text"])

        # 重新计算 confidence
        causal_count = sum(1 for e in existing_evidences if e.get("type") == "causal")
        total_count = len(existing_evidences)
        has_counter = any(c in (new_factor.get("not_applicable") or []) for c in ["反例", "不适用"])
        if has_counter or new_factor.get("confidence") == "contested":
            confidence = "contested"
        elif causal_count >= 2:
            confidence = "high"
        elif causal_count >= 1 or total_count >= 2:
            confidence = "medium"
        else:
            confidence = "low"

        # 构造合并后的 factor dict
        merged = dict(new_factor)
        merged["evidence"] = existing_evidences
        merged["confidence"] = confidence
        return merged

    def _render_factor(self, factor: dict, key: str) -> str:
        """将 factor dict 渲染为 markdown 文件内容。"""
        subject     = factor.get("factor_subject", "")
        dimension   = factor.get("factor_dimension", "")
        factor_type = factor.get("factor_type", "model_factors")
        evidences   = factor.get("evidence", [])

        evidence_lines = []
        for e in evidences:
            if isinstance(e, dict):
                tag = "[causal] " if e.get("type") == "causal" else ""
                evidence_lines.append(f"- {tag}{e['text']}")
            else:
                evidence_lines.append(f"- {e}")

        applicable     = "\n".join(f"- {c}" for c in (factor.get("applicable_conditions") or []))
        not_applicable = "\n".join(f"- {c}" for c in (factor.get("not_applicable") or []))

        return f"""---
factor_type: {factor_type}
factor_key: {key}
factor_subject: {subject}
factor_dimension: {dimension}
confidence: {factor.get("confidence", "low")}
evidence_count: {len(evidences)}
last_updated: {date.today().isoformat()}
---

## 规律
{factor.get("rule", "")}

## 证据
{chr(10).join(evidence_lines) or "（无）"}

## 适用条件
{applicable or "- （待明确）"}

## 不适用
{not_applicable or "- （待明确）"}"""

    def _write_insights(self, insights: dict):
        insights_dir = KB_ROOT / "insights"
        insights_dir.mkdir(parents=True, exist_ok=True)

        for key in ("model_comparison", "text_strategy_summary", "prompt_pairing_summary"):
            content = insights.get(key, "")
            if content:
                fpath = insights_dir / f"{key}.md"
                fpath.write_text(
                    f"# {key.replace('_', ' ').title()}\n\n"
                    f"更新时间: {date.today().isoformat()}\n\n{content}",
                    encoding="utf-8",
                )


AgentExecutorRegistry.register("kb_aggregation", KBAggregationExecutor)
