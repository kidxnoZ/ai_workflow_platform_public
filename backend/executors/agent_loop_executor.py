"""克隆实验室 v3 Agent Loop 执行器。

替代 prompt_experiment_v3.py 的 phase-dispatch 架构，
使用真正的 agent loop：while → LLM → tool_use → execute → continue。

兼容机制：通过 task_type 仍为 "prompt_experiment_v3"，
在 worker 中根据 input_data 内容（是否有 agent_events）判断走新/旧 executor。
"""
import json
import logging
import time
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from backend.config import (
    ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL, ANTHROPIC_MODEL,
)
from backend.executors.agent_base import AgentBaseExecutor, AgentExecutorRegistry
from backend.executors.agent_tools import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)

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


SYSTEM_PROMPT = """# 角色
你是一个语音克隆策略 Agent。你通过工具调用完成实验，自主决定每一步行动。

# 模态约束
你无法听音频。所有音频质量判断来源于：
- prompt 标注（通过 transcribe_prompts 工具获取 ASR 文本 + 结构化特征标注）
- 用户评价（通过 request_user_review 工具获取排名/评分/标签/备注）
不得基于想象对音频质量做主观判断。

# Prompt 音频特征标注
transcribe_prompts 返回每个 prompt 的 ASR 文本 + 七项结构化特征：
- **gender**：性别（男性 / 女性 / 中性）
- **age**：年龄段（儿童 / 青少年 / 青年 / 中年 / 老年）
- **character**：性格特点（沉稳 / 活泼 / 温柔 / 磁性 等）
- **recording_env**：录制环境（录音棚近场 / 安静室内 / 线上会议 / 户外嘈杂）
- **speaking_pace**：语速节奏（偏快 / 正常 / 偏慢）
- **style**：演绎风格（配音演员演绎 / 自然对话 / 朗读腔 / 综艺感）
- **paralanguage**：副语言量（无 / 少量 / 中量 / 丰富）

如何利用这些特征：
- **recording_env=录音棚近场** → 音质干净，通常带来更高相似度；优先在"提升相似度"目标时选用
- **speaking_pace=偏快** → 韵律密度高，与 mimo 等韵律敏感模型更匹配；用于活泼轻快场景
- **paralanguage=丰富** → 情感表达丰富，适合情感类场景；但可能导致 elevenlabs 等模型稳定性下降
- **设计 diagnostic combo 时**：优先固定 model+text，只改变 prompt 的 recording_env，这样分数差异可归因于录制质量
- **为 qwen-tts 设计 instructions 时**：必须以 gender / age / character 为基础，不可与标注冲突（标注为男性则 instructions 不能写女性音色，反之亦然）

write_experience 时：明确记录用了哪个 prompt 及其特征值，如"p0(录音棚近场, 偏快, 男性, 中年, 磁性) + mimo → 相似度 4.1，韵律自然"，供 KB 聚合提炼 prompt_factors。

# 可用工具
1. **transcribe_prompts** — 转写 prompt 音频，获取 ASR 文本 + 结构化特征标注
2. **query_kb** — 查询知识库（模型档案/历史经验/因子库/覆盖矩阵/历史策略）
3. **design_combos** — 设计实验组合方案（仅规划，不合成，立即返回）
4. **run_synthesis** — 执行当前轮次的音频合成（耗时操作，请在 design_combos 后立即调用）
5. **request_user_review** — 暂停等待用户评价（每轮合成后必须调用）
6. **update_portfolio** — 更新策略档案（每次分析评价后调用）
7. **write_experience** — 写入知识库经验（每轮调用一次小结，结束前调用一次总结）
8. **finish_experiment** — 结束实验
9. **update_model_knowledge** — 追加本次实验对某模型的新认知到 KB profile（可选，结束前有新发现时调用）
10. **distill_experience** — 将本任务最新 experience 同步提炼为 factors/insights（可选，仅在多轮实验且后续还有轮次时调用；单轮实验或最后一轮不要调用）

# query_kb 使用指引
## 推荐工作流
- **任务开始时**：`scope="all"` → 一次性获取完整 KB 视图（profile + experience + factor 目录 + insights + 历史策略）
- **需要深读某因子**：`scope="factors"`, `keywords="{factor_key 或关键词}"` → 获取匹配因子的完整规律和证据
- **只查因子目录**：`scope="factor_index"` → 返回所有因子 key+rule+confidence，不含全文
- **查历史实验策略**：`scope="strategies"` → 返回同场景最近 5 次任务的最终策略档案

## scope 说明
| scope | 返回内容 |
|---|---|
| `all` | profiles + experiences + **factor_index**（目录）+ insights + coverage + strategies |
| `factor_index` | 所有因子的 key、rule、confidence 轻量摘要（不含全文）|
| `factors` | keyword grep 匹配的因子**全文**（需要有意义的 keywords）|
| `profiles` | 模型档案 + tag_tutorial |
| `experiences` | 最近 N 条原始经验（可用 experience_limit 参数控制，默认 3，最大 10）|
| `strategies` | 同场景历史任务最终策略档案 |
| `insights` | 全局横向摘要 |
| `coverage` | 覆盖矩阵 |

注意：`scope=all` 的 factor 部分返回的是**轻量目录**（非全文）。看到感兴趣的因子后，再用 `scope=factors` + 对应 keywords 读取全文。

## KB 经验适用边界（硬性规则，不可违反）
KB 中的因子和经验是**先验参考，不是决策依据**。FOLLOWING KB NEGATIVE CONCLUSIONS WITHOUT VERIFICATION IS FORBIDDEN：
- **禁止因 KB 因子而跳过任何模型能力测试**：如 KB 说"mimo 标签破坏相似度"，你仍必须用标签设计 exploit combo，因为：
  - 该因子 confidence=contested（有反例），仅作参考
  - 不同场景/不同 prompt 结论可能完全相反
  - TTS 生成有随机性，几次负面不代表能力不可用
- **confidence 含义**：`high`(≥2 causal 无反例) > `medium`(≥2 correlational 或 1 causal) > `low`(仅1条证据) > `contested`(存在反例)
  - `low`/`contested` 因子**禁止**作为排除某方案的理由
  - 即使 `high`，如果当前场景与因子适用条件不同，也不应直接套用
- **因子描述倾向，不判定可行性**："风格标签可能影响相似度"是风险提示 ≠ "不要用标签"
- **正确做法**：怀疑 KB 结论时，用 exploit combo 测试该能力，用 diagnostic combo 做对照验证
- **新实验可以推翻旧结论**：这才是实验的意义

# 工作流程（建议顺序，可自主调整）
1. transcribe_prompts → 了解 prompt 内容
2. query_kb(scope="all") → 查阅完整 KB 视图；如发现相关因子，再用 scope=factors 读全文
3. design_combos → 设计第一轮实验方案（组合 × 文本 ≤ {max_synthesis_per_round}）
4. run_synthesis → 执行合成（紧跟 design_combos，不要插入其他工具）
5. request_user_review → 等待用户评价
6. 分析评价 → update_portfolio → write_experience（本轮策略分析）
   → [可选] distill_experience（仅当后续还有第 N+1 轮时调用，让下一轮能查到结构化因子；最后一轮不调）
   → 判断是否收敛
7. 如未收敛 → 回到步骤 3 设计下一轮
8. 收敛或用户满意或 finish_requested：
   → write_experience（完整实验总结）
   → [可选] update_model_knowledge（有新模型认知时，每个模型一次）
   → finish_experiment

# write_experience 内容要求
每轮小结和最终总结都要围绕**策略因果分析**，核心是记录：
- 每个策略组合（模型 + 参数 + prompt 选择 + 文本策略）对目标场景产生的效果
- 用户标记的缺点标签（如"相似度低"、"情感平淡"）归因到哪个因子
- 用户备注中的关键判断（直接引用原文）
- 哪些策略在什么条件下有效/失效，以及推测原因
- 跨轮对比：同一策略在不同轮次的表现变化及可能原因

不要复述原始排名数字，而是分析排名背后的原因。
不要只写结论，要写"策略 X 导致了问题 Y，证据是用户标记了 Z"这样的因果链。

# 用户终止信号
如果评价结果中包含 "finish_requested": true，表示用户希望结束实验：
1. 分析当前评价 → update_portfolio（确保最终 portfolio 准确）
2. write_experience（完整总结）
3. [可选] update_model_knowledge
4. finish_experiment
不要再设计新一轮实验。

# 组合设计原则
每轮组合同时服务三个目标：
- **exploit**：选历史表现最好的组合（多数槽位）
- **diagnostic**：控制变量对照（少数，验证假设）
- **explore**：覆盖盲区（1-2 个）

硬性约束：
- combos × base_texts ≤ {max_synthesis_per_round}（用户设置的单轮上限）
- 每个 diagnostic 对只变一个因子
- 当 auto_text_count > 0 时，base_texts 条数必须等于 {auto_text_count}（用户明确指定）；当 auto_text_count = 0 时，base_texts 条数由你自行决定（通常 2-4 条）
- base_texts 保留 1-2 条"锚定文本"（跨轮相同，确保可比性）
- 自行生成 base_texts 时每条不超过 40 字，句子完整自然；用户已提供文本时不限

diagnostic 填写规范：
- diagnostic 槽位数 = min(2, floor(总槽位 × 0.3))，根据 KB 中待验证假设数量决定
- 必须填 `control_var`（正在测试哪个变量：model / prompt / text / params）
- 必须填 `baseline_combo`（对照基准 combo_id，除 control_var 指定的变量外，其余字段与本 diagnostic combo 完全相同）
- 例：验证 p0 vs p1 的影响 → baseline_combo 用 p0 的 exploit combo，diagnostic combo 换成 p1，其余（model、text）不变

write_experience 的 control_evidence 填写：
- 每当本轮有 diagnostic combo 且用户已评价，必须填写 `control_evidence` 字段
- 从评价结果中找到 diagnostic combo 与其 baseline_combo 的实际得分，计算差值
- 这是唯一能做因果归因的证据来源，聚合时会被标记为 causal（比 correlational 证据权重更高）

# Prompt 语义与实验目标
用户上传的多个 prompt 音频**通常来自同一说话人**的不同录音（不同内容、不同场景、不同录制条件），而非不同说话人。
- 实验核心目标：**找出哪条 prompt 音频的克隆效果最好**（最优参考音频选择）
- 次要目标：对比不同模型在同一说话人上的表现差异
- 因此 exploit combo 应围绕"同一模型 × 不同 prompt"设计，让用户在同一模型内横向对比各 prompt 的克隆质量
- 评价时用户关注的维度：音色相似度、自然度、情感保留、稳定性——这些都与参考音频质量直接相关
- 如果用户标注了 prompt 的 recording_env / speaking_pace / style 等信息，应利用这些维度分析哪类录音条件更适合做参考音频

# Prompt 全覆盖原则
每轮实验必须让所有提供的 prompt 音频都参与对比。具体要求：
- 每个 prompt（p0, p1, …）必须至少出现在一个 exploit 类型的 combo 中
- 如果 prompt 数量 ≤ 模型数量，可以为每个模型分配不同的 prompt 作为 exploit combo
- 如果 prompt 数量 > 模型数量，则同一模型可以有多条不同 prompt 的 exploit combo
- diagnostic combo 可以复用已有 prompt，但不应以省略某个 prompt 为代价
- 目的：确保每轮评价中所有 prompt 都有合成样本，用户能横向对比不同 prompt 的效果

# 模型全覆盖原则
每轮实验必须让用户勾选的所有候选模型都参与对比。具体要求：
- 每个候选模型必须至少出现在一个 exploit 类型的 combo 中
- 如果模型数量 ≤ prompt 数量，可以为每个 prompt 分配不同模型
- 如果模型数量 > prompt 数量，则同一 prompt 可以搭配多个模型（每个模型一条 combo）
- 目的：确保每轮评价中所有模型都有合成样本，用户能横向对比不同模型的表现
- 例外：如果某模型有已知硬性限制（如 minimax/qwen-tts 要求 prompt ≥ 10s）且当前所有 prompt 均不满足，可以不为该模型分配 combo，但必须在 reasoning 中说明原因

# 文本标签化适配（tag_tutorial）
不同模型支持不同的文本标签/控制语法（如情感标签、停顿标记、发音纠正等）。
- 在 design_combos 前调用 query_kb(scope="profiles") 获取各模型的 tag_tutorial
- **所有 exploit combo 都必须为支持标签的模型设置 text_variants**——这不是诊断实验，而是让每个模型在其最佳输入格式下合成
- 标签化是模型的**正常使用方式**，不是"特殊功能"：不加标签 ≈ 没用上该模型的核心能力
- 变体原则：**语义和策略意图与 base_texts 相同，仅按模型标签格式改写**
- 例：base_text="这么多年过去了" → mimo 变体="(怅然)这么多年过去了" → minimax 变体="这么多年过去了(jue2)色"
- 如果某模型的 tag_tutorial 明确写了"不需要标签"（如 doubao），则该模型不设 text_variants，直接用 base_texts 原文
- diagnostic combo 如需测试"有标签 vs 无标签"的效果差异，可以额外加一个无标签版本，但 exploit 必须带标签
- 目的：对比的是模型在其最佳输入格式下的真实能力，而非"裸文本 vs 标签文本"的格式差异

# Prompt 音频策略
系统会根据 combo 中的 prompt_ids 自动处理音频拼接/选择：
- 单条 prompt < 10s 且多条总长 < 30s → 自动拼接
- 单条 prompt > 20s → 自动裁剪
- **cosyvoice3 硬性限制**：prompt 不得超过 30s，超长时系统自动截取但质量可能下降
  - 为 cosyvoice3 设计组合时，优先选择单条较短的 prompt（< 15s 最佳）
  - 如果只有长 prompt 可用，考虑不为 cosyvoice3 分配多 prompt 拼接组合
- **minimax / qwen-tts-plus / qwen-tts-flash 硬性限制**：克隆用的 prompt 音频（拼接后）必须 ≥ 10 秒，否则服务端直接拒绝
  - 为这三类模型设计组合时，只选时长 ≥ 10s 的 prompt，或选多条拼接后总长 ≥ 10s 的组合
  - 若所有可用 prompt 均 < 10s（无法拼接到 10s），不要为该组合分配这三个模型
  - transcribe_prompts 返回的 duration 字段包含每条 prompt 的时长，据此判断
- text_variants 字段类型为 object（{model_id: text}），不要传 array

# 策略档案（Portfolio）管理
实验目标不是找"一个最优"，而是建立"策略档案"：
- **稳定型**：跨文本排名方差小，适合通用场景
- **偏科型**：在特定文本类型上表现突出
- 每轮分析后更新 portfolio，标注每个策略的 profile 和 best_for

# 评分档位语义（5 级 vs_target）
- 不可用（1_unusable）：音频有明显瑕疵，完全不能商用
- 差距明显（2_gap）：能听出目标方向，但差距大
- 有潜力（3_potential）：部分维度达标，值得调优
- 达到预期（4_expected）：满足商用门槛
- 超出预期（5_exceed）：超越目标要求

# 收敛判断
以下条件满足任一即可建议结束：
- top-3 策略在连续 2 轮锚定文本上排名不变
- 所有 portfolio 策略评分 ≥ 4_expected
- 用户明确表示满意

# 评价解读
- 排名 > 绝对分数：rank=1 意味着比其他好
- issues 标签 > 评分数字
- 用户 notes 是最高信息密度来源
- 多文本交叉分析：跨文本排名方差小 = 稳定策略

# 冷启动处理
如果知识库无相关经验：
1. 依赖模型 profile 的官方描述
2. 第一轮更发散，多测组合，少做判断
3. 明确标注"首次探索该场景，本轮重在收集数据"
4. 探索位比例提升至 40%"""


class AgentLoopExecutor(AgentBaseExecutor):
    MAX_TURNS_PER_RESUME = 50
    MAX_TOTAL_TURNS = 200
    MAX_MESSAGES_SIZE = 80000

    def execute(self, task, db: Session) -> None:
        input_data = json.loads(task.input or "{}")

        if "_agent_messages" in input_data:
            self._resume(task, input_data, db)
        else:
            self._start(task, input_data, db)

    def _start(self, task, input_data: dict, db: Session):
        messages = self._build_initial_messages(input_data)
        input_data["phase"] = "agent_running"
        input_data["agent_events"] = []
        input_data["round"] = 0
        input_data["_total_turns"] = 0
        self._emit_event(input_data, "message", text="Agent 启动，正在分析任务…")
        task.append_log("Agent loop 启动")
        self._save_phase_data(task, input_data, db)
        self._run_loop(task, input_data, messages, db)

    def _resume(self, task, input_data: dict, db: Session):
        messages = input_data.pop("_agent_messages")
        tool_call_id = input_data.pop("_pending_tool_call_id")
        partial_tool_results = input_data.pop("_partial_tool_results", [])

        user_eval = input_data.get("pending_evaluation", {})
        # Combine any tool_results from before request_user_review + the real user eval
        all_results = partial_tool_results + [{
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": json.dumps(user_eval, ensure_ascii=False),
        }]
        messages.append({"role": "user", "content": all_results})

        input_data["phase"] = "agent_running"
        self._emit_event(input_data, "message", text="收到评价，Agent 正在分析…")
        task.append_log(f"Agent loop 恢复（用户评价已注入）")
        self._save_phase_data(task, input_data, db)
        self._run_loop(task, input_data, messages, db)

    def _run_loop(self, task, input_data: dict, messages: list, db: Session):
        turn = input_data.get("_total_turns", 0)
        local_turns = 0

        while local_turns < self.MAX_TURNS_PER_RESUME and turn < self.MAX_TOTAL_TURNS:
            turn += 1
            local_turns += 1
            input_data["_total_turns"] = turn

            max_synth = input_data.get("max_synthesis_per_round", 30)
            auto_tc = input_data.get("auto_text_count", 0)
            content_blocks, tool_uses = self._call_llm_streaming(
                messages, max_synth, auto_tc, task, input_data, db
            )

            if not tool_uses:
                input_data.pop("_streaming", None)
                task.append_log("Agent 停止调用工具（异常终止）", "warn")
                db.commit()
                break

            messages.append({"role": "assistant", "content": self._content_to_dicts_from_raw(content_blocks)})

            tool_results = []
            for tool_block in tool_uses:
                result_str, should_pause = execute_tool(
                    tool_block.name, tool_block.input, task, input_data, db
                )
                # Parse result for structured frontend rendering
                try:
                    result_data = json.loads(result_str)
                except (json.JSONDecodeError, TypeError):
                    result_data = {"raw": result_str[:2000]}
                self._emit_event(input_data, "tool_result",
                                 tool_name=tool_block.name,
                                 tool_result_summary=result_str[:200],
                                 tool_result_data=result_data)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": result_str,
                })

                # Save after each tool so frontend sees results immediately
                self._save_phase_data(task, input_data, db)

                if should_pause:
                    # Store partial results (everything EXCEPT the pause tool's result)
                    # so resume can combine them with the real user evaluation
                    partial = [r for r in tool_results if r["tool_use_id"] != tool_block.id]
                    # Add placeholder results for any tools after the pause tool that won't be executed
                    pause_idx = tool_uses.index(tool_block)
                    for remaining in tool_uses[pause_idx + 1:]:
                        partial.append({
                            "type": "tool_result",
                            "tool_use_id": remaining.id,
                            "content": '{"skipped": "paused_for_user_review"}',
                        })
                    input_data["_partial_tool_results"] = partial
                    self._save_phase_data(task, input_data, db)
                    self._pause_for_review(task, input_data, messages, tool_block.id, db)
                    return

                if tool_block.name == "finish_experiment":
                    self._save_phase_data(task, input_data, db)
                    return

            messages.append({"role": "user", "content": tool_results})

            if self._messages_size(messages) > self.MAX_MESSAGES_SIZE:
                messages = self._summarize_messages(messages)
                task.append_log("Context 压缩完成")
                db.commit()

        if turn >= self.MAX_TOTAL_TURNS:
            task.append_log(f"达到最大轮次 {self.MAX_TOTAL_TURNS}，强制结束", "warn")
            self._force_finish(task, input_data, db)

    def _call_llm(self, messages: list, max_synthesis_per_round: int = 30, auto_text_count: int = 0):
        client = _get_client()
        system = SYSTEM_PROMPT.replace("{max_synthesis_per_round}", str(max_synthesis_per_round))\
                               .replace("{auto_text_count}", str(auto_text_count))
        kwargs = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 8000,
            "system": system,
            "messages": messages,
            "tools": TOOL_DEFINITIONS,
            "timeout": 300,
        }
        # DeepSeek V4 Pro supports thinking
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 5000}

        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            if "thinking" in str(e).lower():
                del kwargs["thinking"]
                return client.messages.create(**kwargs)
            raise

    def _call_llm_streaming(self, messages: list, max_synthesis_per_round: int,
                            auto_text_count: int,
                            task, input_data: dict, db: Session):
        """Streaming LLM call — emits and saves events as each content block completes."""
        client = _get_client()
        system = SYSTEM_PROMPT.replace("{max_synthesis_per_round}", str(max_synthesis_per_round))\
                               .replace("{auto_text_count}", str(auto_text_count))
        kwargs = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 8000,
            "system": system,
            "messages": messages,
            "tools": TOOL_DEFINITIONS,
            "timeout": 300,
            "stream": True,
        }
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 5000}

        try:
            return self._consume_stream(client.messages.create(**kwargs),
                                        task, input_data, db)
        except Exception as e:
            if "thinking" in str(e).lower():
                del kwargs["thinking"]
                return self._consume_stream(client.messages.create(**kwargs),
                                            task, input_data, db)
            # If streaming not supported, fall back to non-streaming
            if "stream" in str(e).lower():
                del kwargs["stream"]
                kwargs.pop("thinking", None)
                response = client.messages.create(**kwargs)
                return self._response_to_blocks(response, task, input_data, db)
            raise

    def _consume_stream(self, stream, task, input_data: dict, db: Session):
        """Process a streaming response, emitting events as blocks complete."""
        content_blocks = []
        tool_uses = []
        current_type = None
        current_text = ""
        current_tool = {}
        current_block_start_ts = None
        last_save = time.time()

        try:
            for event in stream:
                etype = getattr(event, "type", None)

                if etype == "content_block_start":
                    cb = event.content_block
                    current_type = cb.type
                    current_text = ""
                    current_block_start_ts = datetime.utcnow().isoformat()
                    if current_type == "tool_use":
                        current_tool = {"id": cb.id, "name": cb.name, "input_json": ""}

                elif etype == "content_block_delta":
                    delta = event.delta
                    if current_type == "thinking" and hasattr(delta, "thinking"):
                        current_text += delta.thinking
                    elif current_type == "text" and hasattr(delta, "text"):
                        current_text += delta.text
                    elif current_type == "tool_use" and hasattr(delta, "partial_json"):
                        current_tool["input_json"] += delta.partial_json

                    # Save partial content every ~1s so frontend can show progress
                    now = time.time()
                    if now - last_save >= 0.3 and current_type in ("thinking", "text"):
                        evt_type = "thinking" if current_type == "thinking" else "message"
                        key = "content" if current_type == "thinking" else "text"
                        input_data["_streaming"] = {
                            "type": evt_type, key: current_text, "partial": True,
                            "start_ts": current_block_start_ts,
                        }
                        self._save_phase_data(task, input_data, db)
                        last_save = now

                elif etype == "content_block_stop":
                    input_data.pop("_streaming", None)

                    if current_type == "thinking":
                        block = SimpleNamespace(type="thinking", thinking=current_text)
                        content_blocks.append(block)
                        self._emit_event(input_data, "thinking", content=current_text, start_ts=current_block_start_ts)
                    elif current_type == "text":
                        block = SimpleNamespace(type="text", text=current_text)
                        content_blocks.append(block)
                        self._emit_event(input_data, "message", text=current_text, start_ts=current_block_start_ts)
                    elif current_type == "tool_use":
                        try:
                            tool_input = json.loads(current_tool["input_json"]) if current_tool["input_json"] else {}
                        except json.JSONDecodeError:
                            tool_input = {}
                        block = SimpleNamespace(
                            type="tool_use", id=current_tool["id"],
                            name=current_tool["name"], input=tool_input
                        )
                        content_blocks.append(block)
                        tool_uses.append(block)
                        self._emit_event(input_data, "tool_call",
                                         tool_name=block.name, tool_input=block.input)

                    self._save_phase_data(task, input_data, db)
                    last_save = time.time()
                    current_type = None

        finally:
            if hasattr(stream, "close"):
                stream.close()

        return content_blocks, tool_uses

    def _response_to_blocks(self, response, task, input_data: dict, db: Session):
        """Fallback: convert a non-streaming response to blocks, emitting events."""
        content_blocks = []
        tool_uses = []
        for block in response.content:
            if block.type == "thinking":
                content_blocks.append(block)
                self._emit_event(input_data, "thinking", content=block.thinking)
            elif block.type == "text":
                content_blocks.append(block)
                self._emit_event(input_data, "message", text=block.text)
            elif block.type == "tool_use":
                content_blocks.append(block)
                tool_uses.append(block)
                self._emit_event(input_data, "tool_call",
                                 tool_name=block.name, tool_input=block.input)
            self._save_phase_data(task, input_data, db)
        return content_blocks, tool_uses

    def _content_to_dicts_from_raw(self, blocks: list) -> list:
        """Convert SimpleNamespace blocks from streaming into message dicts."""
        result = []
        for block in blocks:
            if block.type == "thinking":
                result.append({"type": "thinking", "thinking": block.thinking})
            elif block.type == "text":
                result.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                result.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input,
                })
        return result

    def _build_initial_messages(self, input_data: dict) -> list:
        prompts = input_data.get("prompts", [])
        prompt_desc = "\n".join([
            f"- p{i}: file_id={p.get('file_id','')}, path={p.get('stored_path','')}"
            for i, p in enumerate(prompts)
        ])

        user_texts = input_data.get("user_texts", [])
        texts_section = json.dumps(user_texts, ensure_ascii=False) if user_texts else "无（请自行生成测试文本）"

        params_map = input_data.get("params_map", {})
        params_section = json.dumps(params_map, ensure_ascii=False) if params_map else "{}"

        max_synth = input_data.get("max_synthesis_per_round", 30)
        auto_tc = input_data.get("auto_text_count", 0)

        user_msg = f"""## 实验任务

**场景类型**: {input_data.get('scene_type', '')}
**场景描述**: {input_data.get('scene_description', '')}
**场景细节**: {json.dumps(input_data.get('scene_form', {}), ensure_ascii=False)}

**Prompt 音频文件**（共 {len(prompts)} 条）:
{prompt_desc}

**候选模型**: {', '.join(input_data.get('model_ids', []))}
**模型参数**: {params_section}

**用户提供文本**: {texts_section}

**单轮合成上限**: combos × base_texts ≤ {max_synth}
**自动生成文本条数**: {auto_tc if auto_tc > 0 else '不限（由 Agent 自行决定，通常 2-4 条）'}

请开始实验。第一步通常是转写 prompt 音频并查询知识库了解相关经验。"""

        return [{"role": "user", "content": user_msg}]

    def _pause_for_review(self, task, input_data: dict, messages: list,
                          tool_call_id: str, db: Session):
        input_data["_agent_messages"] = messages
        input_data["_pending_tool_call_id"] = tool_call_id
        input_data["phase"] = "awaiting_review"
        task.status = "awaiting_review"
        task.input = json.dumps(input_data, ensure_ascii=False)
        task.updated_at = datetime.utcnow()
        task.append_log("等待用户评价...")
        db.commit()

    def _force_finish(self, task, input_data: dict, db: Session):
        input_data["phase"] = "success"
        portfolio = input_data.get("portfolio", [])
        task.result = json.dumps({
            "reason": "max_turns_reached",
            "portfolio": portfolio,
            "summary": f"达到最大轮次限制（{self.MAX_TOTAL_TURNS}），自动结束",
            "total_rounds": input_data.get("round", 0),
        }, ensure_ascii=False)
        task.status = "success"
        task.finished_at = datetime.utcnow()
        task.input = json.dumps(input_data, ensure_ascii=False)
        db.commit()

    def _emit_event(self, input_data: dict, event_type: str, **kwargs):
        events = input_data.setdefault("agent_events", [])
        event = {"type": event_type, "ts": datetime.utcnow().isoformat(), **kwargs}
        events.append(event)
        if len(events) > 200:
            events[:] = events[-200:]

    def _content_to_dicts(self, content) -> list:
        result = []
        for block in content:
            if hasattr(block, "type"):
                if block.type == "thinking":
                    result.append({"type": "thinking", "thinking": block.thinking})
                elif block.type == "text":
                    result.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    result.append({
                        "type": "tool_use", "id": block.id,
                        "name": block.name, "input": block.input,
                    })
                else:
                    result.append({"type": block.type})
            elif isinstance(block, dict):
                result.append(block)
        return result

    def _messages_size(self, messages: list) -> int:
        return len(json.dumps(messages, ensure_ascii=False))

    def _summarize_messages(self, messages: list) -> list:
        if len(messages) <= 12:
            return messages

        head = messages[:1]
        tail = messages[-10:]
        middle = messages[1:-10]

        summary_parts = []
        for msg in middle:
            content = msg.get("content", "")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_result":
                        c = item.get("content", "")
                        summary_parts.append(f"[tool_result] {c[:80]}")
                    elif item.get("type") == "text":
                        summary_parts.append(f"[agent] {item.get('text', '')[:80]}")
                    elif item.get("type") == "tool_use":
                        summary_parts.append(
                            f"[tool_call] {item.get('name', '')}({json.dumps(item.get('input', ''))[:50]})"
                        )
                    elif item.get("type") == "thinking":
                        summary_parts.append(f"[thinking] {item.get('thinking', '')[:60]}")
            elif isinstance(content, str) and content:
                summary_parts.append(f"[msg] {content[:80]}")

        summary_text = "\n".join(summary_parts[-40:])
        summary_msg = {
            "role": "user",
            "content": f"[CONTEXT SUMMARY - 以下是之前 {len(middle)} 轮对话的压缩摘要]\n{summary_text}",
        }
        return head + [summary_msg] + tail


# Register as a new task type so legacy can still use the old executor
AgentExecutorRegistry.register("prompt_experiment_v3_agent", AgentLoopExecutor)
