import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Input,
  Modal,
  Popconfirm,
  Rate,
  Row,
  Space,
  Spin,
  Steps,
  Table,
  Tag,
  Typography,
  Upload,
  message,
} from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  ExperimentOutlined,
  LoadingOutlined,
  PlusOutlined,
  SettingOutlined,
  UploadOutlined,
  UserOutlined,
} from '@ant-design/icons'
import {
  api,
  ContextKey5D,
  KBRecord,
  ExperimentInput,
  FileOut,
  ModelInfo,
  ParamSchema,
  PhaseTiming,
  PlanItem,
  SynthesisResult,
  TaskOut,
} from '../api/client'
import BusinessContextTag from '../components/BusinessContextTag'
import AudioPlayer from '../components/AudioPlayer'
import ModelParamsForm from '../components/ModelParamsForm'
import TaskStatusTag from '../components/TaskStatusTag'

const { Title, Text, Paragraph } = Typography
const { TextArea } = Input

const PROBLEM_TAGS = ['音色不像', '停顿不自然', '语速异常', '有吞字', '情感平淡', '杂音/噪声']

const DEFAULT_CTX: ContextKey5D = {
  character_type: '少女',
  emotion_register: '中性',
  content_type: '游戏对话',
  language_style: '口语日常',
  special_req: '无',
}

function initParams(schema: Record<string, ParamSchema>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(schema).map(([k, s]) => [k, s.default]))
}

const STORAGE_KEY = 'promptLabTaskId'

export default function PromptLab() {
  const qc = useQueryClient()

  // ── Task ID 持久化 ────────────────────────────────────────────────────────────
  const [taskId, setTaskId] = useState<string | null>(() => localStorage.getItem(STORAGE_KEY))

  function startNewExperiment() {
    localStorage.removeItem(STORAGE_KEY)
    setTaskId(null)
    setPromptFiles([])
    setSynthesisScene('')
    setTextsInitialized(false)
    setPlanInitialized(false)
  }

  // ── 创建表单状态 ───────────────────────────────────────────────────────────────
  const [promptFiles, setPromptFiles] = useState<{ file: FileOut; asr_text: string }[]>([])
  const [contextTags, setContextTags] = useState<ContextKey5D>(DEFAULT_CTX)
  const [synthesisScene, setSynthesisScene] = useState<string>('')
  const [selectedModelIds, setSelectedModelIds] = useState<string[]>([])
  const [paramsMap, setParamsMap] = useState<Record<string, Record<string, unknown>>>({})
  const [settingModelId, setSettingModelId] = useState<string | null>(null)
  const [testTextCount, setTestTextCount] = useState(10)
  const [submitting, setSubmitting] = useState(false)

  // ── 确认 ASR 状态 ─────────────────────────────────────────────────────────────
  const [asrTexts, setAsrTexts] = useState<string[]>([])
  const [asrInitialized, setAsrInitialized] = useState(false)

  // ── 确认文本状态 ───────────────────────────────────────────────────────────────
  const [editTexts, setEditTexts] = useState<string[]>([''])
  const [textsInitialized, setTextsInitialized] = useState(false)

  // ── 确认规划状态 ───────────────────────────────────────────────────────────────
  const [checkedCombos, setCheckedCombos] = useState<Set<string>>(new Set())
  const [planInitialized, setPlanInitialized] = useState(false)

  // ── 评选状态 ───────────────────────────────────────────────────────────────────
  const [scores, setScores] = useState<Record<string, number>>({})
  const [evalTags, setEvalTags] = useState<Record<string, string[]>>({})
  const [evalNotes, setEvalNotes] = useState<Record<string, string>>({})
  const [winners, setWinners] = useState<Record<string, string>>({})

  // ── 模型查询 ──────────────────────────────────────────────────────────────────
  const { data: models = [] } = useQuery<ModelInfo[]>({
    queryKey: ['models'],
    queryFn: api.getModels,
  })

  useEffect(() => {
    setParamsMap((prev) => {
      const next = { ...prev }
      for (const id of selectedModelIds) {
        if (!next[id]) {
          const m = models.find((m) => m.id === id)
          next[id] = m ? initParams(m.params_schema.properties ?? {}) : {}
        }
      }
      return next
    })
  }, [selectedModelIds, models])

  // ── 任务轮询 ──────────────────────────────────────────────────────────────────
  const { data: task } = useQuery({
    queryKey: ['experiment', taskId],
    queryFn: () => api.getPromptExperiment(taskId!),
    enabled: !!taskId,
    refetchInterval: (q) => {
      const s = (q.state.data as TaskOut | undefined)?.status
      return s === 'pending' || s === 'running' ? 2000 : false
    },
  })

  const input = task?.input as ExperimentInput | undefined
  const phase = input?.phase
  const status = task?.status

  // live timer：running 时每秒刷新，用于 StepTracker 计时
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (status !== 'running') { setNow(Date.now()); return }
    setNow(Date.now())  // 立即刷新，避免首帧用旧值
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [status])

  // ── 历史数据（confirm_plan 阶段加载）
  const { data: _historyData } = useQuery({
    queryKey: ['exp-history', input?.context_key],
    queryFn: () => api.getExperimentHistory(input?.context_key),
    enabled: false,  // KB 查询结果已由 executor 写入 input_data.kb_query_result
  })

  // 初始化 ASR 文本（只初始化一次）
  useEffect(() => {
    if (!asrInitialized && phase === 'confirm_asr' && status === 'awaiting_review') {
      setAsrTexts((input?.prompts ?? []).map((p) => p.asr_text ?? ''))
      setAsrInitialized(true)
    }
  }, [phase, status, input?.prompts, asrInitialized])

  // 初始化文本列表（只初始化一次）
  useEffect(() => {
    if (!textsInitialized && phase === 'confirm_texts' && status === 'awaiting_review') {
      const generated = input?.generated_texts ?? []
      setEditTexts(generated.length > 0 ? generated : [''])
      setTextsInitialized(true)
    }
  }, [phase, status, input?.generated_texts, textsInitialized])

  // 初始化规划勾选（默认全选）
  useEffect(() => {
    if (!planInitialized && phase === 'confirm_plan' && status === 'awaiting_review' && input?.combo_plan) {
      setCheckedCombos(new Set(input.combo_plan.map((c) => `${c.combo_id}__${c.model_id}`)))
      setPlanInitialized(true)
    }
  }, [phase, status, input?.combo_plan, planInitialized])

  // ── 提交创建 ──────────────────────────────────────────────────────────────────
  async function handleCreate() {
    if (promptFiles.length === 0) return message.error('请上传至少一个 Prompt 音频')
    if (selectedModelIds.length === 0) return message.error('请选择至少一个模型')
    setSubmitting(true)
    try {
      const res = await api.createPromptExperiment({
        prompt_file_ids: promptFiles.map((p) => p.file.file_id),
        prompt_texts: Object.fromEntries(promptFiles.map((p) => [p.file.file_id, p.asr_text])),
        business_context: contextTags,
        model_ids: selectedModelIds,
        params_map: paramsMap,
        test_text_count: testTextCount,
        synthesis_scene: synthesisScene.trim() || undefined,
      })
      localStorage.setItem(STORAGE_KEY, res.task_id)
      setTaskId(res.task_id)
    } catch {
      message.error('创建失败')
    } finally {
      setSubmitting(false)
    }
  }

  // ── 确认 ASR ──────────────────────────────────────────────────────────────────
  async function handleConfirmAsr() {
    setSubmitting(true)
    try {
      await api.confirmAsr(taskId!, asrTexts)
      qc.invalidateQueries({ queryKey: ['experiment', taskId] })
    } catch {
      message.error('提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  // ── 确认文本 ──────────────────────────────────────────────────────────────────
  async function handleConfirmTexts() {
    const texts = editTexts.filter((t) => t.trim())
    if (texts.length === 0) return message.error('请至少输入一条测试文本')
    setSubmitting(true)
    try {
      await api.confirmTexts(taskId!, texts)
      qc.invalidateQueries({ queryKey: ['experiment', taskId] })
    } catch {
      message.error('提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  // ── 确认规划 ──────────────────────────────────────────────────────────────────
  async function handleConfirmPlan() {
    const plan = (input?.combo_plan ?? []).filter(
      (c) => checkedCombos.has(`${c.combo_id}__${c.model_id}`),
    )
    if (plan.length === 0) return message.error('请至少保留一个实验组合')
    setSubmitting(true)
    try {
      await api.confirmPlan(taskId!, plan)
      qc.invalidateQueries({ queryKey: ['experiment', taskId] })
    } catch {
      message.error('提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  // ── 提交评选 ──────────────────────────────────────────────────────────────────
  async function handleSubmitReview() {
    const evaluations: Record<string, { score: number; problem_tags: string[]; notes: string }> = {}
    const results = input?.synthesis_results ?? []
    for (const sr of results) {
      const ek = `${sr.combo_id}__${sr.model_id}__${sr.text_idx}`
      evaluations[ek] = {
        score: scores[ek] ?? 0,
        problem_tags: evalTags[ek] ?? [],
        notes: evalNotes[ek] ?? '',
      }
    }
    setSubmitting(true)
    try {
      await api.submitReview(taskId!, { human_selections: winners, evaluations })
      qc.invalidateQueries({ queryKey: ['experiment', taskId] })
    } catch {
      message.error('提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  // ──────────────────────────────────────────────────────────────────────────────
  // 渲染各阶段
  // ──────────────────────────────────────────────────────────────────────────────

  // 没有 taskId → 创建表单
  if (!taskId) {
    return <CreateForm
      promptFiles={promptFiles} setPromptFiles={setPromptFiles}
      contextTags={contextTags} setContextTags={setContextTags}
      synthesisScene={synthesisScene} setSynthesisScene={setSynthesisScene}
      selectedModelIds={selectedModelIds} setSelectedModelIds={setSelectedModelIds}
      models={models} paramsMap={paramsMap} setParamsMap={setParamsMap}
      settingModelId={settingModelId} setSettingModelId={setSettingModelId}
      testTextCount={testTextCount} setTestTextCount={setTestTextCount}
      onSubmit={handleCreate} submitting={submitting}
    />
  }

  // 有 taskId → 所有阶段共享一个 wrapper（含放弃按钮）
  const stepContent = (() => {
    // 加载中
    if (!task) {
      return <div style={{ padding: 48, textAlign: 'center' }}><Spin tip="加载中..." /></div>
    }

    // 失败
    if (status === 'failed') {
      return (
        <div style={{ padding: 24, maxWidth: 800, margin: '0 auto' }}>
          <Alert type="error" showIcon message="实验失败" description={task.error ?? '未知错误'} />
        </div>
      )
    }

    // 成功
    if (status === 'success') {
      const result = task.result as { jsonl_path: string; total_texts: number; evaluated: number; winner_config: Record<string, unknown> } | null
      return (
        <div style={{ padding: 24, maxWidth: 800, margin: '0 auto' }}>
          <Alert type="success" showIcon message="实验完成！" />
          <Card style={{ marginTop: 16 }}>
            <Descriptions column={2}>
              <Descriptions.Item label="总文本数">{result?.total_texts ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="已评分">{result?.evaluated ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="winner 数">{Object.keys(result?.winner_config ?? {}).length}</Descriptions.Item>
            </Descriptions>
            <Space style={{ marginTop: 16 }}>
              <Button
                type="primary"
                icon={<DownloadOutlined />}
                href={`/api/agent/prompt-experiment/${taskId}/export`}
                download
              >
                下载 experiment_result.jsonl
              </Button>
              <Button onClick={startNewExperiment}>新建实验</Button>
            </Space>
          </Card>
        </div>
      )
    }

    // pending/running + start → ASR 转写中
    if ((status === 'pending' || status === 'running') && phase === 'start') {
      return <SpinnerStep title="正在 ASR 转写..." subtitle="Whisper 识别 Prompt 音频内容" />
    }

    // awaiting_review + confirm_asr → 校对 ASR
    if (status === 'awaiting_review' && phase === 'confirm_asr') {
      const prompts = input?.prompts ?? []
      return (
        <div style={{ padding: 24, maxWidth: 800, margin: '0 auto' }}>
          <Title level={4}>Step 2：校对 ASR 识别结果</Title>
          <Alert
            type="info"
            showIcon
            message="Whisper 识别结果已填入，如有错误请修正后再继续 — AI 生成文本时会参考这些内容"
            style={{ marginBottom: 16 }}
          />
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            {prompts.map((p, i) => (
              <Card
                key={p.file_id}
                size="small"
                title={
                  <Text style={{ fontSize: 12 }}>
                    Prompt {i + 1}：{p.stored_path.split('/').pop()}
                  </Text>
                }
              >
                <Input.TextArea
                  value={asrTexts[i] ?? ''}
                  rows={2}
                  placeholder="ASR 识别结果为空，请手动输入参考文本"
                  onChange={(e) => {
                    const next = [...asrTexts]
                    next[i] = e.target.value
                    setAsrTexts(next)
                  }}
                />
              </Card>
            ))}
          </Space>
          <Space style={{ marginTop: 16 }}>
            <Button type="primary" loading={submitting} onClick={handleConfirmAsr}>
              确认 ASR 文本，开始生成测试文本
            </Button>
          </Space>
        </div>
      )
    }

    // pending/running + confirm_asr → AI 生成文本中
    if ((status === 'pending' || status === 'running') && phase === 'confirm_asr') {
      return <SpinnerStep title="正在生成测试文本..." subtitle="AI 正在根据场景标签生成测试句" />
    }

    // awaiting_review + confirm_texts → 编辑文本
    if (status === 'awaiting_review' && phase === 'confirm_texts') {
      return (
        <div style={{ padding: 24, maxWidth: 800, margin: '0 auto' }}>
          <Title level={4}>Step 2：确认测试文本</Title>
          {(input?.generated_texts ?? []).length === 0 && (
            <Alert
              type="info"
              showIcon
              message="AI 未生成文本（未配置 API Key 或生成失败），请手动输入"
              style={{ marginBottom: 12 }}
            />
          )}
          <Space direction="vertical" style={{ width: '100%' }} size={8}>
            {editTexts.map((t, i) => (
              <Space key={i} style={{ width: '100%' }}>
                <TextArea
                  value={t}
                  rows={2}
                  onChange={(e) => {
                    const next = [...editTexts]
                    next[i] = e.target.value
                    setEditTexts(next)
                  }}
                  style={{ flex: 1, minWidth: 500 }}
                />
                <Button
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => setEditTexts(editTexts.filter((_, j) => j !== i))}
                  disabled={editTexts.length <= 1}
                />
              </Space>
            ))}
            <Button
              type="dashed"
              icon={<PlusOutlined />}
              onClick={() => setEditTexts([...editTexts, ''])}
            >
              添加文本
            </Button>
          </Space>
          <Space style={{ marginTop: 16 }}>
            <Button type="primary" loading={submitting} onClick={handleConfirmTexts}>
              确认文本（{editTexts.filter((t) => t.trim()).length} 条）
            </Button>
          </Space>
        </div>
      )
    }

    // pending/running + confirm_texts → 规划中
    if ((status === 'pending' || status === 'running') && phase === 'confirm_texts') {
      return <SpinnerStep title="正在规划实验组合..." subtitle="切分 Prompt 音频，生成 combo × 模型 矩阵" />
    }

    // awaiting_review + confirm_plan → 确认规划
    if (status === 'awaiting_review' && phase === 'confirm_plan') {
      const plan = input?.combo_plan ?? []
      const kbResult = input?.kb_query_result
      const agentReasoning = input?.agent_reasoning

      const columns = [
        {
          title: '选择',
          key: 'select',
          width: 60,
          render: (_: unknown, row: PlanItem) => {
            const ck = `${row.combo_id}__${row.model_id}`
            return (
              <Checkbox
                checked={checkedCombos.has(ck)}
                onChange={(e) => {
                  const next = new Set(checkedCombos)
                  e.target.checked ? next.add(ck) : next.delete(ck)
                  setCheckedCombos(next)
                }}
              />
            )
          },
        },
        { title: 'Combo', dataIndex: 'label', key: 'label', width: 100 },
        { title: '模型', dataIndex: 'model_id', key: 'model_id', width: 120 },
        {
          title: '参数',
          key: 'params',
          width: 120,
          render: (_: unknown, row: PlanItem) => {
            const entries = Object.entries(row.params ?? {})
            return entries.length > 0 ? entries.map(([k, v]) => `${k}=${v}`).join(', ') : '-'
          },
        },
        {
          title: 'Agent 推荐理由',
          key: 'reasoning',
          render: (_: unknown, row: PlanItem) => (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.reasoning || '-'}
            </Text>
          ),
        },
      ]

      return (
        <div style={{ padding: 24 }}>
          <Title level={4}>Step 3：确认实验规划</Title>
          <Row gutter={16}>
            <Col span={16}>
              <Card size="small" title={`共 ${plan.length} 个组合（取消勾选可跳过）`}>
                <Table
                  dataSource={plan}
                  columns={columns}
                  rowKey={(r) => `${r.combo_id}__${r.model_id}`}
                  size="small"
                  pagination={false}
                />
                <Space style={{ marginTop: 12 }}>
                  <Button type="primary" loading={submitting} onClick={handleConfirmPlan}>
                    开始合成（{checkedCombos.size} 个组合 × {input?.confirmed_texts?.length ?? 0} 条文本）
                  </Button>
                </Space>
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small" title="知识库查询">
                {kbResult ? (
                  <>
                    <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 8 }}>
                      查询 key：{kbResult.query_key}
                    </Text>
                    {kbResult.records.length === 0 ? (
                      <Tag color="orange">无历史数据，建议全量测试</Tag>
                    ) : (
                      kbResult.records.map((r: KBRecord) => (
                        <div key={`${r.prompt_combo}__${r.model_id}`} style={{ marginBottom: 8 }}>
                          <Text strong style={{ fontSize: 12 }}>{r.prompt_combo}</Text>
                          <Tag color="blue" style={{ marginLeft: 6, fontSize: 11 }}>{r.model_id}</Tag>
                          <br />
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            均分 {r.avg_score?.toFixed(1) ?? '-'} / {r.n_samples} 次
                          </Text>
                        </div>
                      ))
                    )}
                  </>
                ) : (
                  <Text type="secondary">-</Text>
                )}
              </Card>
              {agentReasoning && (
                <Card size="small" title="Agent 推理摘要" style={{ marginTop: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>{agentReasoning}</Text>
                </Card>
              )}
            </Col>
          </Row>
        </div>
      )
    }

    // running + confirm_plan → 合成进度
    if (status === 'running' && phase === 'confirm_plan') {
      const prog = input?.synthesis_progress
      const logs = task.logs ?? []
      const recentLogs = logs.slice(-8)
      return (
        <div style={{ padding: 24, maxWidth: 800, margin: '0 auto' }}>
          <Title level={4}>Step 4：合成中</Title>
          {prog && (
            <div style={{ marginBottom: 16 }}>
              <Text>{prog.done} / {prog.total} 个组合完成</Text>
              <div style={{ background: '#f0f0f0', height: 8, borderRadius: 4, marginTop: 8 }}>
                <div
                  style={{
                    width: `${(prog.done / prog.total) * 100}%`,
                    background: '#1890ff',
                    height: '100%',
                    borderRadius: 4,
                    transition: 'width 0.5s',
                  }}
                />
              </div>
            </div>
          )}
          <Card size="small" title="运行日志" style={{ fontFamily: 'monospace', fontSize: 12 }}>
            {recentLogs.map((l, i) => (
              <div key={i} style={{ color: l.level === 'error' ? '#ff4d4f' : '#595959' }}>
                [{l.time.slice(11, 19)}] {l.msg}
              </div>
            ))}
            {!prog && <Spin size="small" style={{ marginRight: 8 }} />}
          </Card>
        </div>
      )
    }

    // awaiting_review + select_winner → 评选
    if (status === 'awaiting_review' && phase === 'select_winner') {
      const results = input?.synthesis_results ?? []
      const textGroups = new Map<string, SynthesisResult[]>()
      for (const r of results) {
        const g = textGroups.get(r.text_idx) ?? []
        g.push(r)
        textGroups.set(r.text_idx, g)
      }

      return (
        <div style={{ padding: 24, maxWidth: 1000, margin: '0 auto' }}>
          <Title level={4}>Step 5：评选 Winner</Title>
          <Space direction="vertical" style={{ width: '100%' }} size={16}>
            {Array.from(textGroups.entries())
              .sort(([a], [b]) => parseInt(a) - parseInt(b))
              .map(([tIdx, group]) => (
                <Card key={tIdx} size="small" title={`文本 ${parseInt(tIdx) + 1}: ${group[0]?.text}`}>
                  <Row gutter={[12, 12]}>
                    {group.map((sr) => {
                      const ek = `${sr.combo_id}__${sr.model_id}__${sr.text_idx}`
                      const wk = `${sr.combo_id}__${sr.model_id}`
                      return (
                        <Col key={ek} xs={24} sm={12} md={8}>
                          <Card
                            size="small"
                            style={{
                              border: winners[tIdx] === wk ? '2px solid #1890ff' : undefined,
                            }}
                            extra={
                              <Button
                                size="small"
                                type={winners[tIdx] === wk ? 'primary' : 'default'}
                                onClick={() => setWinners((prev) => ({ ...prev, [tIdx]: wk }))}
                              >
                                {winners[tIdx] === wk ? 'Winner ✓' : '选为 Winner'}
                              </Button>
                            }
                            title={<Text style={{ fontSize: 12 }}>{sr.combo_id} / {sr.model_id}</Text>}
                          >
                            <AudioPlayer url={sr.audio_url} />
                            <div style={{ marginTop: 8 }}>
                              <Rate
                                value={scores[ek] ?? 0}
                                onChange={(v) => setScores((prev) => ({ ...prev, [ek]: v }))}
                              />
                            </div>
                            <Checkbox.Group
                              options={PROBLEM_TAGS}
                              value={evalTags[ek] ?? []}
                              onChange={(v) => setEvalTags((prev) => ({ ...prev, [ek]: v as string[] }))}
                              style={{ marginTop: 6, fontSize: 12 }}
                            />
                            <Input.TextArea
                              placeholder="备注（可选）"
                              rows={1}
                              value={evalNotes[ek] ?? ''}
                              onChange={(e) => setEvalNotes((prev) => ({ ...prev, [ek]: e.target.value }))}
                              style={{ marginTop: 6, fontSize: 12 }}
                            />
                          </Card>
                        </Col>
                      )
                    })}
                  </Row>
                </Card>
              ))}
          </Space>
          <div style={{ marginTop: 24 }}>
            <Button type="primary" size="large" loading={submitting} onClick={handleSubmitReview}>
              提交评选结果（{Object.keys(winners).length} 个 winner）
            </Button>
          </div>
        </div>
      )
    }

    // pending + select_winner → 写入数据中
    if ((status === 'pending' || status === 'running') && phase === 'select_winner') {
      return <SpinnerStep title="正在写入评选数据..." subtitle="保存评分记录，生成 experiment_result.jsonl" />
    }

    // 兜底
    return <SpinnerStep title="处理中..." subtitle={`status=${status} phase=${phase}`} />
  })()

  return (
    <>
      <div style={{
        padding: '8px 24px',
        borderBottom: '1px solid #f0f0f0',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        background: '#fafafa',
      }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          任务 {taskId?.slice(0, 8)}… · <TaskStatusTag status={status ?? 'pending'} />
        </Text>
        <Popconfirm
          title="放弃当前实验？"
          description="当前进度将不会保存，确定要开始新实验吗？"
          onConfirm={startNewExperiment}
          okText="确定放弃"
          cancelText="继续"
          okButtonProps={{ danger: true }}
        >
          <Button size="small" danger>放弃 / 重新开始</Button>
        </Popconfirm>
      </div>
      <div style={{ display: 'flex', minHeight: 'calc(100vh - 49px)' }}>
        <StepTracker phase={phase} status={status} phaseTiming={input?.phase_timings} now={now} />
        <div style={{ flex: 1, overflow: 'auto', minWidth: 0 }}>
          {stepContent}
        </div>
      </div>
    </>
  )
}

// ── 子组件 ─────────────────────────────────────────────────────────────────────

function SpinnerStep({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div style={{ padding: 48, textAlign: 'center' }}>
      <Spin size="large" />
      <div style={{ marginTop: 16 }}>
        <Title level={5}>{title}</Title>
        {subtitle && <Text type="secondary">{subtitle}</Text>}
      </div>
    </div>
  )
}

// ── StepTracker ────────────────────────────────────────────────────────────────

interface StepDef {
  label: string
  isUserStep: boolean
  timingKey?: string
}

const EXPERIMENT_STEPS: StepDef[] = [
  { label: 'ASR 转写',       isUserStep: false, timingKey: 'start' },
  { label: '校对 ASR 文本',   isUserStep: true },
  { label: 'AI 文本生成',    isUserStep: false, timingKey: 'confirm_asr' },
  { label: '确认测试文本',    isUserStep: true },
  { label: '规划 + AI 推理',  isUserStep: false, timingKey: 'confirm_texts' },
  { label: '确认实验规划',    isUserStep: true },
  { label: '批量合成',       isUserStep: false, timingKey: 'confirm_plan' },
  { label: '评选 Winner',   isUserStep: true },
  { label: '写入评分数据',   isUserStep: false, timingKey: 'select_winner' },
]

function getActiveIndex(phase?: string, status?: string): number {
  if (status === 'success') return 9
  if (status === 'pending' || status === 'running') {
    if (phase === 'start')         return 0
    if (phase === 'confirm_asr')   return 2
    if (phase === 'confirm_texts') return 4
    if (phase === 'confirm_plan')  return 6
    if (phase === 'select_winner') return 8
  }
  if (status === 'awaiting_review') {
    if (phase === 'confirm_asr')   return 1
    if (phase === 'confirm_texts') return 3
    if (phase === 'confirm_plan')  return 5
    if (phase === 'select_winner') return 7
  }
  return 0
}

function fmtMs(ms: number): string {
  const s = Math.floor(ms / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
}

function StepTracker({
  phase, status, phaseTiming, now,
}: {
  phase?: string
  status?: string
  phaseTiming?: Record<string, PhaseTiming>
  now: number
}) {
  const activeIndex = getActiveIndex(phase, status)
  const allDone = status === 'success'

  const items = EXPERIMENT_STEPS.map((step, i) => {
    const isActive = i === activeIndex && !allDone
    const isFinished = i < activeIndex || allDone

    // 描述：机器步骤显示耗时，用户步骤显示状态
    let description: React.ReactNode = null
    if (!step.isUserStep && step.timingKey) {
      const t = phaseTiming?.[step.timingKey]
      if (t?.ended_at && t?.began_at) {
        const ms = new Date(t.ended_at).getTime() - new Date(t.began_at).getTime()
        description = <span style={{ fontSize: 11, color: '#8c8c8c' }}>{fmtMs(ms)}</span>
      } else if (t?.began_at && isActive) {
        const ms = Math.max(0, now - new Date(t.began_at).getTime())
        description = <span style={{ fontSize: 11, color: '#1677ff' }}>{fmtMs(ms)}…</span>
      }
    } else if (step.isUserStep) {
      if (isActive)    description = <span style={{ fontSize: 11, color: '#faad14' }}>等待操作</span>
      else if (isFinished) description = <span style={{ fontSize: 11, color: '#8c8c8c' }}>已确认</span>
    }

    // 图标：当前机器步骤显示 spinner，用户步骤显示 user 图标
    let icon: React.ReactNode | undefined
    if (isActive) {
      icon = step.isUserStep
        ? <UserOutlined style={{ color: '#faad14' }} />
        : <LoadingOutlined style={{ color: '#1677ff' }} />
    }

    return {
      title: <span style={{ fontSize: 13 }}>{step.label}</span>,
      description,
      status: (allDone || isFinished) ? 'finish' as const
            : isActive ? 'process' as const
            : 'wait' as const,
      icon,
    }
  })

  return (
    <div style={{
      width: 210,
      minWidth: 210,
      padding: '20px 16px',
      borderRight: '1px solid #f0f0f0',
      background: '#fafafa',
      alignSelf: 'stretch',
    }}>
      <Text strong style={{ fontSize: 12, color: '#8c8c8c', display: 'block', marginBottom: 16, letterSpacing: 1 }}>
        实验进度
      </Text>
      <Steps direction="vertical" size="small" items={items} style={{ fontSize: 13 }} />
    </div>
  )
}

interface CreateFormProps {
  promptFiles: { file: FileOut; asr_text: string }[]
  setPromptFiles: React.Dispatch<React.SetStateAction<{ file: FileOut; asr_text: string }[]>>
  contextTags: ContextKey5D
  setContextTags: React.Dispatch<React.SetStateAction<ContextKey5D>>
  synthesisScene: string
  setSynthesisScene: React.Dispatch<React.SetStateAction<string>>
  selectedModelIds: string[]
  setSelectedModelIds: React.Dispatch<React.SetStateAction<string[]>>
  models: ModelInfo[]
  paramsMap: Record<string, Record<string, unknown>>
  setParamsMap: React.Dispatch<React.SetStateAction<Record<string, Record<string, unknown>>>>
  settingModelId: string | null
  setSettingModelId: React.Dispatch<React.SetStateAction<string | null>>
  testTextCount: number
  setTestTextCount: React.Dispatch<React.SetStateAction<number>>
  onSubmit: () => void
  submitting: boolean
}

function CreateForm({
  promptFiles, setPromptFiles,
  contextTags, setContextTags,
  synthesisScene, setSynthesisScene,
  selectedModelIds, setSelectedModelIds,
  models, paramsMap, setParamsMap,
  settingModelId, setSettingModelId,
  testTextCount, setTestTextCount,
  onSubmit, submitting,
}: CreateFormProps) {
  const settingModel = models.find((m) => m.id === settingModelId)

  async function uploadPrompt(file: File) {
    try {
      const out = await api.uploadFile(file)
      setPromptFiles((prev) => [...prev, { file: out, asr_text: '' }])
      message.success(`已上传: ${out.original_name}`)
    } catch {
      message.error('上传失败')
    }
    return false
  }

  return (
    <div style={{ padding: 24, maxWidth: 900, margin: '0 auto' }}>
      <Title level={4}>
        <ExperimentOutlined style={{ marginRight: 8 }} />
        克隆实验室 — 新建实验
      </Title>

      <Card title="1. 上传 Prompt 音频" style={{ marginBottom: 16 }}>
        <Upload
          accept="audio/*"
          showUploadList={false}
          beforeUpload={uploadPrompt}
          multiple
        >
          <Button icon={<UploadOutlined />}>上传 Prompt 音频（可多个）</Button>
        </Upload>
        <Space direction="vertical" style={{ width: '100%', marginTop: 12 }} size={8}>
          {promptFiles.map((p, i) => (
            <Space key={p.file.file_id} style={{ width: '100%' }}>
              <Tag>{p.file.original_name}</Tag>
              <Input
                placeholder="Whisper 将自动转写，此处可手动覆盖"
                value={p.asr_text}
                onChange={(e) => {
                  const next = [...promptFiles]
                  next[i] = { ...next[i], asr_text: e.target.value }
                  setPromptFiles(next)
                }}
                style={{ width: 360 }}
              />
              <Button
                type="text"
                danger
                icon={<DeleteOutlined />}
                onClick={() => setPromptFiles(promptFiles.filter((_, j) => j !== i))}
              />
            </Space>
          ))}
        </Space>
      </Card>

      <Card title="2. 业务场景标签（5D）" style={{ marginBottom: 16 }}>
        <BusinessContextTag
          value={contextTags}
          onChange={(_, tags) => setContextTags(tags)}
          mode="edit"
        />
      </Card>

      <Card title="3. 合成场景（可选）" style={{ marginBottom: 16 }}>
        <Input.TextArea
          placeholder="例如：赛博朋克风格都市场景，剧情对话台词。留空则按原音频风格生成。"
          value={synthesisScene}
          onChange={(e) => setSynthesisScene(e.target.value)}
          rows={2}
          style={{ width: '100%' }}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          填写后，AI 生成的测试文本将匹配此目标场景，而非模仿原音频内容。
        </Text>
      </Card>

      <Card title="4. 选择测试模型" style={{ marginBottom: 16 }}>
        <Row gutter={[8, 8]}>
          {models.map((m) => (
            <Col key={m.id}>
              <Space>
                <Checkbox
                  checked={selectedModelIds.includes(m.id)}
                  onChange={(e) => {
                    setSelectedModelIds(
                      e.target.checked
                        ? [...selectedModelIds, m.id]
                        : selectedModelIds.filter((id) => id !== m.id),
                    )
                  }}
                  disabled={m.status !== 'active'}
                >
                  {m.display_name}
                </Checkbox>
                {selectedModelIds.includes(m.id) && (
                  <Button
                    size="small"
                    type="text"
                    icon={<SettingOutlined />}
                    onClick={() => setSettingModelId(m.id)}
                  />
                )}
              </Space>
            </Col>
          ))}
        </Row>
      </Card>

      <Card title="5. 生成文本数量" style={{ marginBottom: 16 }}>
        <Space>
          <Text>让 Claude 生成</Text>
          <Input
            type="number"
            value={testTextCount}
            min={1}
            max={30}
            onChange={(e) => setTestTextCount(parseInt(e.target.value) || 10)}
            style={{ width: 80 }}
          />
          <Text>条测试文本（无 API KEY 时可手动输入）</Text>
        </Space>
      </Card>

      <Button type="primary" size="large" loading={submitting} onClick={onSubmit}>
        创建实验
      </Button>

      <Modal
        title={`${settingModel?.display_name} 参数设置`}
        open={!!settingModelId}
        onCancel={() => setSettingModelId(null)}
        footer={<Button type="primary" onClick={() => setSettingModelId(null)}>确定</Button>}
      >
        {settingModel && (
          <ModelParamsForm
            schema={settingModel.params_schema.properties ?? {}}
            values={paramsMap[settingModelId!] ?? {}}
            onChange={(key, value) =>
              setParamsMap((prev) => ({
                ...prev,
                [settingModelId!]: { ...(prev[settingModelId!] ?? {}), [key]: value },
              }))
            }
          />
        )}
      </Modal>
    </div>
  )
}
