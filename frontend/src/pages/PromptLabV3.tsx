import { useEffect, useRef, useState, useMemo, useCallback } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  Upload,
  Progress,
  message,
} from 'antd'
import {
  DeleteOutlined,
  ExperimentOutlined,
  HolderOutlined,
  LeftOutlined,
  LoadingOutlined,
  PlusOutlined,
  RocketOutlined,
  SettingOutlined,
  SortAscendingOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  api,
  AgentEvent,
  FileOut,
  ModelInfo,
  ParamSchema,
  PortfolioItem,
  TaskOut,
  V3ExperimentInput,
  V3SynthesisResult,
} from '../api/client'
import AudioPlayer from '../components/AudioPlayer'
import ModelParamsForm from '../components/ModelParamsForm'
import TaskStatusTag from '../components/TaskStatusTag'
import dayjs from 'dayjs'

const { Title, Text } = Typography
const { TextArea } = Input

const SCENE_TYPES = [
  { label: '角色演绎', children: ['游戏剧情台词', '动漫/动画配音', '有声书角色', '广播剧', '虚拟主播'] },
  { label: '表演娱乐', children: ['脱口秀/单口喜剧', '相声', '综艺主持', '直播带货', '搞笑配音'] },
  { label: '日常对话', children: ['闲聊对话', '客服应答', '教学讲解', '采访回答', '语音消息'] },
  { label: '叙事朗读', children: ['有声书旁白', '播客独白', '纪录片解说', '课文/诗歌朗诵', '冥想引导'] },
  { label: '正式播报', children: ['新闻播报', '会议纪要播报'] },
  { label: '营销推广', children: ['短视频口播', '品牌广告', '产品介绍', '活动预告', '种草推荐'] },
  { label: '功能交互', children: ['导航语音', '智能助手', 'IVR 语音菜单', '通知播报', '无障碍朗读'] },
  { label: '情感表达', children: ['情感电台', '祝福/告白', '哀悼/慰问', '鼓励/激励', 'ASMR/耳语'] },
  { label: '其他', children: [] },
]
const ISSUE_TAGS = ['相似度低', '不自然', '情感平淡', '情感过度', '语速不对', '有词错误', '停顿异常']
const VS_TARGET_OPTIONS = [
  { value: '1_unusable', label: '不可用' },
  { value: '2_gap', label: '差距明显' },
  { value: '3_potential', label: '有潜力' },
  { value: '4_expected', label: '达到预期' },
  { value: '5_exceed', label: '超出预期' },
]

function initParams(schema: Record<string, ParamSchema>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(schema).map(([k, s]) => [k, s.default]))
}

// ── 任务列表视图 ──────────────────────────────────────────────────────────────

function ExperimentList() {
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data: historyData, isLoading } = useQuery({
    queryKey: ['experiment-v3-history'],
    queryFn: () => api.getExperimentV3History(),
    refetchInterval: 5000,
  })

  const experiments = historyData?.items ?? []

  const columns = [
    {
      title: '时间', dataIndex: 'created_at', key: 'created_at', width: 130,
      render: (v: string) => dayjs(v).format('MM-DD HH:mm:ss'),
    },
    {
      title: '场景', key: 'scene_type', width: 160,
      render: (_: unknown, row: TaskOut) => {
        const input = row.input as Record<string, unknown> | undefined
        return <Tag>{(input?.scene_type as string) || '—'}</Tag>
      },
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 100,
      render: (v: string) => <TaskStatusTag status={v} />,
    },
    {
      title: '轮次', key: 'round', width: 70,
      render: (_: unknown, row: TaskOut) => {
        const input = row.input as Record<string, unknown> | undefined
        const round = input?.round as number | undefined
        return round ? `第 ${round} 轮` : '—'
      },
    },
    {
      title: '操作', key: 'action', width: 160,
      render: (_: unknown, row: TaskOut) => (
        <Space size={4}>
          <Button size="small" type="primary" onClick={() => navigate(`/prompt-lab/${row.id}`)}>
            查看
          </Button>
          {['pending', 'running', 'awaiting_review'].includes(row.status) && (
            <Popconfirm title="终止此任务？" okText="终止" cancelText="取消" okButtonProps={{ danger: true }}
              onConfirm={async () => {
                try { await api.cancelExperimentV3(row.id); qc.invalidateQueries({ queryKey: ['experiment-v3-history'] }); message.success('已终止') } catch { message.error('终止失败') }
              }}>
              <Button size="small" danger type="link">终止</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <Title level={4} style={{ margin: 0 }}>
          <ExperimentOutlined style={{ marginRight: 8 }} />
          克隆实验室
        </Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/prompt-lab/new')}>
          新建实验
        </Button>
      </div>
      <Card>
        {isLoading ? (
          <div style={{ textAlign: 'center', padding: 24 }}><Spin size="large" /></div>
        ) : experiments.length === 0 ? (
          <div style={{ textAlign: 'center', padding: 48, color: '#aaa' }}>
            暂无实验记录，点击右上角「新建实验」开始
          </div>
        ) : (
          <Table
            size="small"
            dataSource={experiments}
            columns={columns}
            rowKey="id"
            pagination={{ pageSize: 20 }}
            onRow={(row: TaskOut) => ({ onClick: () => navigate(`/prompt-lab/${row.id}`), style: { cursor: 'pointer' } })}
          />
        )}
      </Card>
    </div>
  )
}

// ── 主组件：根据 mode 路由到列表 / 创建 / 运行 ─────────────────────────────

export default function PromptLabV3({ mode }: { mode: 'list' | 'create' | 'run' }) {
  const navigate = useNavigate()
  const params = useParams<{ taskId?: string }>()

  const qc = useQueryClient()
  const taskId = mode === 'run' ? (params.taskId ?? null) : null

  function startNew() {
    navigate('/prompt-lab/new')
  }

  // ── 创建表单状态 ─────────────────────────────────────────────────────────────
  type PromptAnnotation = {
    recording_env: string
    speaking_pace: string
    style: string
    paralanguage: string
    gender: string
    age: string
    character: string
  }
  const DEFAULT_ANNOTATION: PromptAnnotation = {
    recording_env: '', speaking_pace: '', style: '', paralanguage: '',
    gender: '', age: '', character: '',
  }
  const [promptFiles, setPromptFiles] = useState<{ file: FileOut; asr_text: string; annotation: PromptAnnotation }[]>([])
  const [sceneType, setSceneType] = useState('')
  const [sceneSubType, setSceneSubType] = useState('')
  const [sceneDescription, setSceneDescription] = useState('')
  const [sceneForm, setSceneForm] = useState<Record<string, unknown>>({})
  const [selectedModelIds, setSelectedModelIds] = useState<string[]>([])
  const [paramsMap, setParamsMap] = useState<Record<string, Record<string, unknown>>>({})
  const [settingModelId, setSettingModelId] = useState<string | null>(null)
  const [userTexts, setUserTexts] = useState<string[]>([])
  const [autoTextCount, setAutoTextCount] = useState<number>(0)
  const [maxSynthesisPerRound, setMaxSynthesisPerRound] = useState<number>(30)
  const [submitting, setSubmitting] = useState(false)
  const [synthLogs, setSynthLogs] = useState<{ time: string; level: string; msg: string }[]>([])

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
    queryKey: ['experiment-v3', taskId],
    queryFn: () => api.getExperimentV3(taskId!),
    enabled: !!taskId,
    refetchInterval: (q) => {
      const s = (q.state.data as TaskOut | undefined)?.status
      return s === 'pending' || s === 'running' ? 2000 : false
    },
  })

  const input = task?.input as V3ExperimentInput | undefined
  const phase = input?.phase
  const status = task?.status

  // mode=list → 任务列表（所有 hooks 必须在此之前调用，不可提前 return）
  if (mode === 'list') return <ExperimentList />

  // ── 提交创建 ──────────────────────────────────────────────────────────────────
  async function handleCreate() {
    if (promptFiles.length === 0) return message.error('请上传至少一个 Prompt 音频')
    if (selectedModelIds.length === 0) return message.error('请选择至少一个模型')
    setSubmitting(true)
    try {
      const res = await api.createExperimentV3({
        prompt_file_ids: promptFiles.map((p) => p.file.file_id),
        prompt_annotations: Object.fromEntries(
          promptFiles.map((p) => [p.file.file_id, p.annotation])
        ),
        scene_type: sceneSubType ? `${sceneType} / ${sceneSubType}` : sceneType,
        scene_description: sceneDescription,
        scene_form: sceneForm,
        model_ids: selectedModelIds,
        params_map: paramsMap,
        user_texts: userTexts.filter((t) => t.trim()),
        max_synthesis_per_round: maxSynthesisPerRound,
        auto_text_count: autoTextCount,
      })
      navigate(`/prompt-lab/${res.task_id}`)
    } catch {
      message.error('创建失败')
    } finally {
      setSubmitting(false)
    }
  }

  // ── 没有 taskId → 创建表单 ────────────────────────────────────────────────────
  if (!taskId) {
    const settingModel = models.find((m) => m.id === settingModelId)
    return (
      <div style={{ padding: 24, maxWidth: 900, margin: '0 auto' }}>
        <div style={{ marginBottom: 16 }}>
          <Button icon={<LeftOutlined />} onClick={() => navigate('/prompt-lab')}>返回列表</Button>
        </div>

        <Card title="1. 上传 Prompt 音频" style={{ marginBottom: 16 }}>
          <Upload accept="audio/*" showUploadList={false} multiple
            beforeUpload={async (file) => {
              try {
                const out = await api.uploadFile(file)
                setPromptFiles((prev) => [...prev, { file: out, asr_text: '', annotation: DEFAULT_ANNOTATION }])
              } catch { message.error('上传失败') }
              return false
            }}>
            <Button icon={<UploadOutlined />}>上传 Prompt 音频（可多个）</Button>
          </Upload>
          <Space direction="vertical" style={{ width: '100%', marginTop: 12 }} size={8}>
            {promptFiles.map((p, i) => {
              const ann = p.annotation
              const setAnn = (patch: Partial<typeof ann>) =>
                setPromptFiles((prev) => prev.map((x, j) =>
                  j === i ? { ...x, annotation: { ...x.annotation, ...patch } } : x
                ))
              const audioUrl = `/api/files/${p.file.file_id}/download`
              return (
                <Card key={p.file.file_id} size="small" style={{ borderColor: '#e8e8e8' }}
                  title={
                    <Space>
                      <span style={{ fontWeight: 500, fontSize: 13 }}>p{i} · {p.file.original_name}</span>
                      <Button type="text" danger size="small" icon={<DeleteOutlined />}
                        onClick={() => setPromptFiles(promptFiles.filter((_, j) => j !== i))} />
                    </Space>
                  }
                  extra={
                    <audio controls src={audioUrl}
                      style={{ height: 28, maxWidth: 220 }} />
                  }>
                  <Row gutter={[12, 8]}>
                    {([
                      { key: 'gender',        label: '性别',    options: ['男性', '女性', '中性'] },
                      { key: 'age',           label: '年龄段',  options: ['儿童(5-12)', '青少年(13-18)', '青年(19-35)', '中年(36-55)', '老年(55+)'] },
                      { key: 'character',     label: '性格特点', options: ['沉稳', '活泼', '温柔', '干练', '磁性', '甜美', '低沉', '清澈'] },
                      { key: 'recording_env', label: '录制环境', options: ['录音棚近场', '安静室内', '线上会议', '户外/嘈杂'] },
                      { key: 'speaking_pace', label: '语速节奏', options: ['偏快', '正常', '偏慢'] },
                      { key: 'style',         label: '演绎风格', options: ['配音演员演绎', '自然对话', '朗读腔', '综艺感'] },
                      { key: 'paralanguage',  label: '副语言量', options: ['无', '少量', '中量', '丰富'] },
                    ] as { key: keyof PromptAnnotation; label: string; options: string[] }[]).map(({ key, label, options }) => (
                      <Col key={key} span={12}>
                        <div style={{ fontSize: 12, color: '#595959', marginBottom: 2 }}>{label}</div>
                        <Select
                          style={{ width: '100%' }}
                          size="small"
                          allowClear
                          placeholder="选择或输入"
                          showSearch
                          value={ann[key] || undefined}
                          onChange={(v) => setAnn({ [key]: v ?? '' })}
                          onSearch={(v) => { if (v && !options.includes(v)) setAnn({ [key]: v }) }}
                          options={options.map((o) => ({ label: o, value: o }))}
                          filterOption={false}
                        />
                      </Col>
                    ))}
                  </Row>
                </Card>
              )
            })}
          </Space>
        </Card>

        <Card title="2. 目标场景" style={{ marginBottom: 16 }}>
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            <div>
              <Text strong>场景类型：</Text>
              <Select value={sceneType || undefined}
                onChange={(v) => { setSceneType(v); setSceneSubType('') }}
                style={{ width: 140, marginLeft: 8 }}
                placeholder="一级分类"
                options={SCENE_TYPES.map((g) => ({ label: g.label, value: g.label }))} />
              {sceneType && (() => {
                const group = SCENE_TYPES.find((g) => g.label === sceneType)
                return group && group.children.length > 0 ? (
                  <Select value={sceneSubType || undefined}
                    onChange={setSceneSubType}
                    style={{ width: 160, marginLeft: 8 }}
                    allowClear
                    placeholder="二级分类（可选）"
                    options={group.children.map((c) => ({ label: c, value: c }))} />
                ) : null
              })()}
            </div>
            <div>
              <Text strong>目标场景：</Text>
              <TextArea placeholder="描述目标场景，越具体越好。如：AI狼人杀中的诚恳直率女声，句子短，有对话感"
                value={sceneDescription} onChange={(e) => setSceneDescription(e.target.value)}
                rows={2} style={{ marginTop: 4 }} />
            </div>
            <div>
              <Text strong>角色设定（context_prompt，可选）：</Text>
              <TextArea placeholder="输入给模型的角色设定/上下文（支持的模型会透传此参数）"
                value={(sceneForm.context_prompt as string) || ''}
                onChange={(e) => setSceneForm({ ...sceneForm, context_prompt: e.target.value })}
                rows={2} style={{ marginTop: 4 }} />
            </div>
          </Space>
        </Card>

        <Card title="3. 选择测试范围" extra={<Text type="secondary" style={{ fontSize: 12 }}>每轮实验会尽量使用所有 Prompt 和模型进行并行对比</Text>} style={{ marginBottom: 16 }}>
          <Row gutter={[8, 8]}>
            {[...models].sort((a, b) => {
              const aApi = a.model_type === 'api' ? 0 : 1
              const bApi = b.model_type === 'api' ? 0 : 1
              return aApi - bApi
            }).map((m) => {
              const isLocal = m.model_type !== 'api'
              return (
                <Col key={m.id}>
                  <Space>
                    <Checkbox
                      checked={selectedModelIds.includes(m.id)}
                      disabled={isLocal}
                      style={{ color: isLocal ? '#aaa' : undefined }}
                      onChange={(e) => setSelectedModelIds(
                        e.target.checked ? [...selectedModelIds, m.id] : selectedModelIds.filter((id) => id !== m.id)
                      )}>
                      {m.display_name}
                    </Checkbox>
                    {selectedModelIds.includes(m.id) && (
                      <Button size="small" type="text" icon={<SettingOutlined />}
                        onClick={() => setSettingModelId(m.id)} />
                    )}
                  </Space>
                </Col>
              )
            })}
          </Row>
          <Text type="secondary" style={{ fontSize: 11, marginTop: 6, display: 'block' }}>
            灰色模型需要本地部署，当前环境不可用
          </Text>
        </Card>

        <Card title="4. 合成文本" style={{ marginBottom: 16 }}>
          <Space direction="vertical" style={{ width: '100%' }} size={8}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Text strong>自动生成条数：</Text>
              <InputNumber min={0} max={10} value={autoTextCount}
                onChange={(v) => setAutoTextCount(v ?? 0)} />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {autoTextCount > 0 ? `Agent 将自动生成 ${autoTextCount} 条测试文本` : '0 = 不限，Agent 自行决定（通常 2-4 条）'}
              </Text>
            </div>
            <div style={{ borderTop: '1px dashed #f0f0f0', paddingTop: 8, marginTop: 4 }}>
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                手动输入文本（可选，优先级高于自动生成）：
              </Text>
              {userTexts.map((t, i) => (
                <Space key={i} style={{ width: '100%', marginBottom: 4 }}>
                  <Input value={t} placeholder="输入测试文本"
                    onChange={(e) => { const n = [...userTexts]; n[i] = e.target.value; setUserTexts(n) }}
                    style={{ width: 500 }} />
                  <Button type="text" danger icon={<DeleteOutlined />}
                    onClick={() => setUserTexts(userTexts.filter((_, j) => j !== i))} />
                </Space>
              ))}
              <Button type="dashed" icon={<PlusOutlined />} onClick={() => setUserTexts([...userTexts, ''])}>
                添加文本
              </Button>
              {userTexts.length > 0 && (
                <Text type="warning" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
                  已手动输入文本，自动生成条数不生效
                </Text>
              )}
            </div>
          </Space>
        </Card>

        <Card title="5. 高级设置" style={{ marginBottom: 16 }}>
          <Space align="center">
            <Text>单轮最大合成数（combos × texts）：</Text>
            <InputNumber min={6} max={100} value={maxSynthesisPerRound}
              onChange={(v) => setMaxSynthesisPerRound(v ?? 30)} />
            <Text type="secondary" style={{ fontSize: 12 }}>默认 30，增大会延长每轮等待时间</Text>
          </Space>
        </Card>

        <Button type="primary" size="large" loading={submitting} onClick={handleCreate}
          icon={<RocketOutlined />}>
          创建实验
        </Button>

        <Modal title={`${settingModel?.display_name} 参数设置`}
          open={!!settingModelId} onCancel={() => setSettingModelId(null)}
          footer={<Button type="primary" onClick={() => setSettingModelId(null)}>确定</Button>}>
          {settingModel && (
            <ModelParamsForm schema={settingModel.params_schema.properties ?? {}}
              values={paramsMap[settingModelId!] ?? {}}
              onChange={(key, value) => setParamsMap((prev) => ({
                ...prev, [settingModelId!]: { ...(prev[settingModelId!] ?? {}), [key]: value },
              }))} />
          )}
        </Modal>
      </div>
    )
  }

  // ── 有 taskId → 渲染各阶段 ────────────────────────────────────────────────────

  const mainContent = (() => {
    if (!task) return <SpinnerStep title="加载中..." />
    if (status === 'failed') {
      return <Alert type="error" showIcon message="实验失败" description={task.error ?? '未知错误'}
        style={{ margin: 24, maxWidth: 800 }} />
    }
    return <AgentRunningPanel task={task} input={input!} taskId={taskId} qc={qc}
      onNewExperiment={startNew} synthLogs={synthLogs} models={models} />
  })()

  return (
    <>
      <div style={{ padding: '8px 24px', borderBottom: '1px solid #f0f0f0', display: 'flex', justifyContent: 'space-between', alignItems: 'center', background: '#fafafa' }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          任务 {taskId?.slice(0, 8)}… · <TaskStatusTag status={status ?? 'pending'} />
          {input?.round ? ` · 第 ${input.round} 轮` : ''}
          <Button size="small" type="link" icon={<LeftOutlined />} onClick={() => navigate('/prompt-lab')} style={{ marginLeft: 8 }}>
            列表
          </Button>
        </Text>
        <Space size={8}>
          {status && !['success', 'failed'].includes(status) && (
            <Popconfirm title="终止当前任务？" onConfirm={async () => {
              try { await api.cancelExperimentV3(taskId); qc.invalidateQueries({ queryKey: ['experiment-v3', taskId] }); message.success('已终止') } catch { message.error('终止失败') }
            }} okText="终止" cancelText="取消" okButtonProps={{ danger: true }}>
              <Button size="small" danger>终止任务</Button>
            </Popconfirm>
          )}
          <Popconfirm title="放弃当前实验？" onConfirm={() => navigate('/prompt-lab')} okText="确定" cancelText="继续" okButtonProps={{ danger: true }}>
            <Button size="small" danger>返回列表</Button>
          </Popconfirm>
        </Space>
      </div>
      <div style={{ display: 'flex', minHeight: 'calc(100vh - 49px)' }}>
        <AgentTimeline events={input?.agent_events ?? []} isRunning={status === 'running'}
          taskId={taskId} onSynthLog={(logs) => setSynthLogs(prev => [...prev.slice(-200), ...logs])} />
        <div style={{ flex: 1, overflow: 'auto', minWidth: 0 }}>
          {mainContent}
        </div>
      </div>
    </>
  )
}

// ── Sub Components ────────────────────────────────────────────────────────────

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

// ── Agent Timeline (v3.3) — SSE Streaming thought log ──────────────────────────

function AgentTimeline({ events: polledEvents, isRunning, taskId, onSynthLog }: {
  events: AgentEvent[]; isRunning: boolean; taskId: string
  onSynthLog?: (logs: { time: string; level: string; msg: string }[]) => void
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const [expandedIdx, setExpandedIdx] = useState<Set<number>>(new Set())
  const [isAtBottom, setIsAtBottom] = useState(true)
  const [lastSeenCount, setLastSeenCount] = useState(0)
  const [sseEvents, setSseEvents] = useState<AgentEvent[]>([])
  const [streamingPartial, setStreamingPartial] = useState<{ type: string; content?: string; text?: string; start_ts?: string } | null>(null)
  const [nowMs, setNowMs] = useState(Date.now())
  const qc = useQueryClient()

  // Live clock for elapsed timer
  useEffect(() => {
    if (!isRunning) return
    const id = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(id)
  }, [isRunning])

  // SSE connection for real-time event streaming
  useEffect(() => {
    if (!taskId) return
    const es = new EventSource(`/api/agent/prompt-experiment-v3/${taskId}/stream`)
    es.addEventListener('events', (e) => {
      try {
        const newEvents = JSON.parse(e.data) as AgentEvent[]
        setSseEvents((prev) => [...prev, ...newEvents])
        setStreamingPartial(null)
      } catch { /* ignore parse errors */ }
      // Trigger right panel refresh immediately
      qc.invalidateQueries({ queryKey: ['experiment-v3', taskId] })
    })
    es.addEventListener('streaming', (e) => {
      try {
        const data = JSON.parse(e.data)
        setStreamingPartial(data)
      } catch { /* ignore */ }
    })
    es.addEventListener('state', () => {
      qc.invalidateQueries({ queryKey: ['experiment-v3', taskId] })
    })
    es.addEventListener('done', (e) => {
      try {
        const data = JSON.parse(e.data) as { status?: string }
        // 只在任务真正结束时关闭 SSE；awaiting_review 时后端会继续保持连接
        if (data.status === 'success' || data.status === 'failed') {
          es.close()
        }
      } catch {
        es.close()
      }
      setStreamingPartial(null)
      qc.invalidateQueries({ queryKey: ['experiment-v3', taskId] })
    })
    es.addEventListener('error', () => {
      // 不主动 close()，让浏览器 EventSource 自动重连
      // 只有在任务已完成时才真正关闭
      if (es.readyState === EventSource.CLOSED) return
    })
    es.addEventListener('synthesis_progress', () => {
      qc.invalidateQueries({ queryKey: ['experiment-v3', taskId] })
    })
    es.addEventListener('synthesis_log', (e) => {
      try {
        const logs = JSON.parse(e.data) as { time: string; level: string; msg: string }[]
        if (logs.length > 0) onSynthLog?.(logs)
      } catch { /* ignore */ }
    })
    return () => es.close()
  }, [taskId, qc])

  // Use SSE events if they're ahead of polled events, otherwise use polled
  const events = sseEvents.length >= polledEvents.length ? sseEvents : polledEvents
  // Sync SSE state when poll catches up (avoid drift)
  useEffect(() => {
    if (polledEvents.length > sseEvents.length) {
      setSseEvents(polledEvents)
    }
  }, [polledEvents, sseEvents.length])

  // Deduplicate events by ts+type+tool_name+content (SSE reconnect can push duplicates)
  const dedupedEvents = useMemo(() => {
    const seen = new Set<string>()
    return events.filter(ev => {
      const key = `${ev.ts}:${ev.type}:${ev.tool_name ?? ''}:${(ev.content ?? ev.text ?? '').slice(0, 30)}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
  }, [events])

  const handleScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40
    setIsAtBottom(atBottom)
    if (atBottom) setLastSeenCount(dedupedEvents.length)
  }

  useEffect(() => {
    if (isAtBottom && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
      setLastSeenCount(dedupedEvents.length)
    }
  }, [dedupedEvents.length, isAtBottom, streamingPartial])

  const scrollToBottom = () => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
      setIsAtBottom(true)
      setLastSeenCount(dedupedEvents.length)
    }
  }

  const unreadCount = dedupedEvents.length - lastSeenCount

  const TOOL_ICONS: Record<string, string> = {
    transcribe_prompts: '🎤', query_kb: '📚', design_combos: '🎯', run_synthesis: '🎵',
    request_user_review: '⏸', update_portfolio: '📊', write_experience: '✍',
    finish_experiment: '🏁',
  }

  // ── Timing helpers ───────────────────────────────────────────────────────────
  const parseTs = (ts: string) => new Date(ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z').getTime()

  const startTs = dedupedEvents.length > 0 ? parseTs(dedupedEvents[0].ts) : null

  const fmtElapsed = (ms: number) => {
    const s = Math.floor(ms / 1000)
    if (s < 60) return `${s}s`
    return `${Math.floor(s / 60)}m${s % 60}s`
  }

  // Compute net LLM elapsed + per-event phase durations
  const { netElapsedMs, pendingToolStartMs, phaseDurations } = useMemo(() => {
    const empty = { netElapsedMs: 0, pendingToolStartMs: null, phaseDurations: new Map<number, number>() }
    if (!startTs) return empty
    // FIFO queue: tool_name → [{callIdx, callMs}]
    const queue = new Map<string, Array<{ callIdx: number; callMs: number }>>()
    let totalToolMs = 0
    const phaseDurs = new Map<number, number>()  // event index → phase duration ms

    for (let i = 0; i < dedupedEvents.length; i++) {
      const ev = dedupedEvents[i]
      const evMs = parseTs(ev.ts)

      if (ev.type === 'tool_call' && ev.tool_name) {
        const arr = queue.get(ev.tool_name) ?? []
        arr.push({ callIdx: i, callMs: evMs })
        queue.set(ev.tool_name, arr)
      } else if (ev.type === 'tool_result' && ev.tool_name) {
        const arr = queue.get(ev.tool_name)
        if (arr && arr.length > 0) {
          const { callIdx, callMs } = arr.shift()!
          if (arr.length === 0) queue.delete(ev.tool_name)
          const dur = evMs - callMs
          totalToolMs += dur
          phaseDurs.set(callIdx, dur)   // tool_call index: how long the tool ran
          // tool_result: no duration shown (it's a result, not a process)
        }
      } else if (ev.type === 'thinking' || ev.type === 'message') {
        if (ev.start_ts) {
          // use actual block duration recorded by backend (content_block_start → stop)
          phaseDurs.set(i, evMs - parseTs(ev.start_ts))
        } else {
          const nextEv = dedupedEvents[i + 1]
          if (nextEv) phaseDurs.set(i, parseTs(nextEv.ts) - evMs)
        }
      }
    }

    const allPending = [...queue.values()].flatMap(a => a.map(x => x.callMs))
    const pendingStart = allPending.length > 0 ? Math.min(...allPending) : null
    const lastEvMs = dedupedEvents.length > 0 ? parseTs(dedupedEvents[dedupedEvents.length - 1].ts) : startTs
    return {
      netElapsedMs: Math.max(0, lastEvMs - startTs - totalToolMs),
      pendingToolStartMs: pendingStart,
      phaseDurations: phaseDurs,
    }
  }, [dedupedEvents, startTs])

  const toggleExpand = (idx: number) => {    setExpandedIdx((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx)
      else next.add(idx)
      return next
    })
  }

  return (
    <div style={{ width: 280, minWidth: 280, borderRight: '1px solid #f0f0f0', background: '#fafafa', display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: '12px 12px 8px', borderBottom: '1px solid #f0f0f0', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Text strong style={{ fontSize: 11, color: '#8c8c8c', letterSpacing: 1 }}>
          Agent 思考过程
        </Text>
        <span style={{ fontSize: 11, color: '#8c8c8c', fontVariantNumeric: 'tabular-nums' }}>
          {isRunning && startTs && fmtElapsed(nowMs - startTs)}
          {isRunning && <LoadingOutlined style={{ marginLeft: 6, color: '#1890ff' }} />}
          {!isRunning && startTs && (
            <span style={{ color: '#52c41a' }}>✓ {fmtElapsed(
              dedupedEvents.length > 0
                ? parseTs(dedupedEvents[dedupedEvents.length - 1].ts) - startTs
                : 0
            )}</span>
          )}
        </span>
      </div>
      <div style={{ flex: 1, position: 'relative' }}>
        <div ref={scrollRef} onScroll={handleScroll} style={{ position: 'absolute', inset: 0, overflowY: 'auto', padding: '8px 10px' }}>
          {dedupedEvents.map((ev, i) => {
            const isLast = i === dedupedEvents.length - 1
            const isExpanded = expandedIdx.has(i) || (isLast && isRunning)
            const evMs = parseTs(ev.ts)
            // Per-event phase duration: how long THIS phase took (not cumulative from start)
            const phaseDur = phaseDurations.has(i)
              ? fmtElapsed(phaseDurations.get(i)!)
              : (isLast && isRunning && !streamingPartial && ev.type !== 'tool_result')
                ? fmtElapsed(Math.max(0, nowMs - evMs))  // live ticking only when no streaming block
                : null

            if (ev.type === 'thinking') {
              const text = ev.content ?? ''
              const preview = text.length > 60 ? text.slice(0, 60) + '…' : text
              return (
                <div key={i} style={{ marginBottom: 6, cursor: 'pointer' }} onClick={() => toggleExpand(i)}>
                  <div style={{ padding: '4px 8px', background: '#f9f0ff', borderRadius: 4, borderLeft: '3px solid #722ed1' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: 10, color: '#722ed1', fontWeight: 600 }}>
                        🧠 思考 {!isExpanded && <span style={{ fontWeight: 400, color: '#8c8c8c' }}>▸</span>}
                      </span>
                      {phaseDur && <span style={{ fontSize: 10, color: '#d3adf7', fontVariantNumeric: 'tabular-nums' }}>{phaseDur}</span>}
                    </div>
                    {isExpanded ? (
                      <div style={{ fontSize: 11, color: '#595959', lineHeight: '17px', whiteSpace: 'pre-wrap', marginTop: 2 }}>
                        {text}
                      </div>
                    ) : (
                      <div style={{ fontSize: 11, color: '#8c8c8c', lineHeight: '15px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {preview}
                      </div>
                    )}
                  </div>
                </div>
              )
            }

            if (ev.type === 'tool_call') {
              const icon = TOOL_ICONS[ev.tool_name ?? ''] ?? '🔧'
              return (
                <div key={i} style={{ marginBottom: 4, padding: '3px 8px', background: '#e6f7ff', borderRadius: 4, borderLeft: '3px solid #1890ff' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontSize: 11, fontWeight: 600, color: '#1890ff' }}>
                      {icon} {ev.tool_name}
                    </span>
                    {phaseDur && <span style={{ fontSize: 10, color: '#91caff', fontVariantNumeric: 'tabular-nums' }}>{phaseDur}</span>}
                  </div>
                </div>
              )
            }

            if (ev.type === 'tool_result') {
              const summary = ev.tool_result_summary ?? ''
              const shortSummary = summary.length > 60 ? summary.slice(0, 60) + '…' : summary
              const hasDetail = summary.length > 60 || !!ev.tool_result_data
              return (
                <div key={i} style={{ marginBottom: 4, paddingLeft: 14, cursor: hasDetail ? 'pointer' : 'default' }}
                  onClick={() => hasDetail && toggleExpand(i)}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontSize: 10, color: '#52c41a' }}>
                      ✓ {ev.tool_name ?? '完成'}
                      {hasDetail && <span style={{ color: '#8c8c8c', marginLeft: 4 }}>{isExpanded ? '▾' : '▸'}</span>}
                    </span>
                  </div>
                  {isExpanded ? (
                    <div style={{ fontSize: 10, color: '#595959', marginTop: 2, lineHeight: '15px', whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto', background: '#f6f6f6', padding: '4px 6px', borderRadius: 3 }}>
                      {summary || JSON.stringify(ev.tool_result_data, null, 2)?.slice(0, 1000)}
                    </div>
                  ) : (
                    shortSummary && (
                      <div style={{ fontSize: 10, color: '#8c8c8c', marginTop: 1, lineHeight: '14px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {shortSummary}
                      </div>
                    )
                  )}
                </div>
              )
            }

            if (ev.type === 'message') {
              const text = ev.text ?? ''
              const preview = text.length > 80 ? text.slice(0, 80) + '…' : text
              return (
                <div key={i} style={{ marginBottom: 6, cursor: text.length > 80 ? 'pointer' : 'default' }} onClick={() => text.length > 80 && toggleExpand(i)}>
                  <div style={{ padding: '4px 8px', background: '#f6ffed', borderRadius: 4, borderLeft: '3px solid #52c41a' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: 10, color: '#389e0d', fontWeight: 600 }}>💬 输出</span>
                      {phaseDur && <span style={{ fontSize: 10, color: '#95de64', fontVariantNumeric: 'tabular-nums' }}>{phaseDur}</span>}
                    </div>
                    <div style={{ fontSize: 11, color: '#595959', lineHeight: '17px', whiteSpace: 'pre-wrap' }}>
                      {isExpanded ? text : preview}
                    </div>
                  </div>
                </div>
              )
            }
            if (ev.type === 'system_event') {
              return (
                <div key={i} style={{ marginBottom: 6 }}>
                  <div style={{ padding: '3px 8px', background: '#e6f4ff', borderRadius: 4, borderLeft: '3px solid #1677ff' }}>
                    <span style={{ fontSize: 10, color: '#0958d9' }}>⚙ {ev.content}</span>
                  </div>
                </div>
              )
            }
            return null
          })}
          {/* Show streaming partial content (currently generating) */}
          {streamingPartial && (
            <div style={{ marginBottom: 6 }}>
              {streamingPartial.type === 'thinking' ? (
                <div style={{ padding: '4px 8px', background: '#f9f0ff', borderRadius: 4, borderLeft: '3px solid #722ed1', opacity: 0.8 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontSize: 10, color: '#722ed1', fontWeight: 600 }}>
                      🧠 思考中… <LoadingOutlined style={{ fontSize: 9 }} />
                    </span>
                    {streamingPartial.start_ts && (
                      <span style={{ fontSize: 10, color: '#d3adf7', fontVariantNumeric: 'tabular-nums' }}>
                        {fmtElapsed(Math.max(0, nowMs - parseTs(streamingPartial.start_ts)))}
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: 11, color: '#595959', lineHeight: '17px', whiteSpace: 'pre-wrap', marginTop: 2 }}>
                    {streamingPartial.content ?? ''}
                  </div>
                </div>
              ) : (
                <div style={{ padding: '4px 8px', background: '#f6ffed', borderRadius: 4, borderLeft: '3px solid #52c41a', opacity: 0.8 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontSize: 10, color: '#389e0d', fontWeight: 600 }}>
                      💬 输出中… <LoadingOutlined style={{ fontSize: 9 }} />
                    </span>
                    {streamingPartial.start_ts && (
                      <span style={{ fontSize: 10, color: '#95de64', fontVariantNumeric: 'tabular-nums' }}>
                        {fmtElapsed(Math.max(0, nowMs - parseTs(streamingPartial.start_ts)))}
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: 11, color: '#595959', lineHeight: '17px', whiteSpace: 'pre-wrap', marginTop: 2 }}>
                    {streamingPartial.text ?? ''}
                  </div>
                </div>
              )}
            </div>
          )}
          {isRunning && dedupedEvents.length === 0 && !streamingPartial && (
            <div style={{ textAlign: 'center', padding: 20, color: '#bfbfbf' }}>
              <LoadingOutlined style={{ fontSize: 20 }} /><br />
              <Text type="secondary" style={{ fontSize: 11 }}>Agent 启动中…</Text>
            </div>
          )}
        </div>
        {!isAtBottom && (
          <div
            onClick={scrollToBottom}
            style={{ position: 'absolute', bottom: 8, left: '50%', transform: 'translateX(-50%)', background: '#1890ff', color: '#fff', borderRadius: 12, padding: '2px 12px', fontSize: 11, cursor: 'pointer', boxShadow: '0 2px 8px rgba(0,0,0,.15)', zIndex: 10 }}
          >
            ↓ 最新{unreadCount > 0 && ` (${unreadCount})`}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Stage Cards — structured rendering of tool results ──────────────────────

interface StageCard {
  title: string
  icon: string
  toolName: string
  toolInput?: unknown
  toolResultData?: unknown
  ts: string
  round: number
}

interface RoundGroup {
  round: number
  label: string
  cards: StageCard[]
  hasEvaluation: boolean      // 该轮有已完成的 request_user_review
  evaluationCardIdx: number   // evaluation page 插入位置（前 N 张是评价前，后面是评价后分析）
}

function buildStageCards(events: AgentEvent[]): { cards: StageCard[]; rounds: RoundGroup[] } {
  const cards: StageCard[] = []
  const TITLES: Record<string, string> = {
    transcribe_prompts: 'ASR 转写结果',
    query_kb: '知识库查询',
    design_combos: '实验设计',
    run_synthesis: '音频合成',
    update_portfolio: '策略档案更新',
    write_experience: '经验写入',
    finish_experiment: '实验完成',
    update_model_knowledge: '模型知识更新',
    distill_experience: '经验提炼',
  }
  const ICONS: Record<string, string> = {
    transcribe_prompts: '🎤', query_kb: '📚', design_combos: '🎯',
    run_synthesis: '🎵',
    update_portfolio: '📊', write_experience: '✍', finish_experiment: '🏁',
    update_model_knowledge: '🧠', distill_experience: '⚗',
  }

  // 从 1 开始，去掉"准备阶段"
  let currentRound = 1
  let successfulDesignCombos = 0   // 只有成功的 design_combos 才触发轮次递增
  const pendingCalls: Map<string, Array<{ input: unknown; ts: string }>> = new Map()
  const evaluatedRounds = new Set<number>()
  // 每轮评价触发时该轮已有的卡片数，用于把 evaluation page 插到正确位置（synthesis 后、post-eval 分析前）
  const evaluationCardIdxByRound = new Map<number, number>()

  for (const ev of events) {
    if (ev.type === 'tool_call' && ev.tool_name === 'design_combos') {
      const queue = pendingCalls.get('design_combos') ?? []
      queue.push({ input: ev.tool_input, ts: ev.ts })
      pendingCalls.set('design_combos', queue)
    } else if (ev.type === 'tool_call' && ev.tool_name && ev.tool_name !== 'request_user_review') {
      const queue = pendingCalls.get(ev.tool_name) ?? []
      queue.push({ input: ev.tool_input, ts: ev.ts })
      pendingCalls.set(ev.tool_name, queue)
    } else if (ev.type === 'tool_result' && ev.tool_name === 'request_user_review') {
      evaluatedRounds.add(currentRound)
      // 记录此时该轮已有的卡片数，evaluation page 插在这之后
      evaluationCardIdxByRound.set(currentRound, cards.filter(c => c.round === currentRound).length)
      pendingCalls.clear()
    } else if (ev.type === 'tool_result' && ev.tool_name === 'design_combos') {
      // 只在 design_combos 成功时递增轮次（跳过参数错误等失败的调用）
      const data = ev.tool_result_data as Record<string, unknown> | undefined
      const isSuccessful = data && !data.error && (data.combos_count ?? (data as Record<string, unknown>).round)
      if (isSuccessful) {
        successfulDesignCombos++
        if (successfulDesignCombos > 1) currentRound++
      }
      // 正常消费 pending call
      const queue = pendingCalls.get('design_combos')
      if (queue && queue.length > 0) queue.shift()
      if (queue && queue.length === 0) pendingCalls.delete('design_combos')
      cards.push({
        title: '实验设计',
        icon: '🎯',
        toolName: 'design_combos',
        toolInput: queue?.[0]?.input ?? ev.tool_input,
        toolResultData: ev.tool_result_data,
        ts: ev.ts,
        round: currentRound,
      })
    } else if (ev.type === 'tool_result' && ev.tool_name && ev.tool_name !== 'request_user_review' && ev.tool_name !== 'design_combos') {
      const name = ev.tool_name
      const queue = pendingCalls.get(name) ?? []
      const pending = queue.shift()  // FIFO: take first matching call
      if (queue.length === 0) pendingCalls.delete(name)
      cards.push({
        title: TITLES[name] ?? name,
        icon: ICONS[name] ?? '🔧',
        toolName: name,
        toolInput: pending?.input ?? ev.tool_input,
        toolResultData: ev.tool_result_data,
        ts: ev.ts,
        round: currentRound,
      })
    }
  }

  // Create in-progress cards for tool_calls that haven't received tool_result yet
  for (const [name, queue] of pendingCalls.entries()) {
    for (const pending of queue) {
      cards.push({
        title: TITLES[name] ?? name,
        icon: ICONS[name] ?? '🔧',
        toolName: name,
        toolInput: pending.input,
        toolResultData: undefined,
        ts: pending.ts,
        round: currentRound,
      })
    }
  }

  const roundMap = new Map<number, StageCard[]>()
  for (const c of cards) {
    const g = roundMap.get(c.round) ?? []
    g.push(c)
    roundMap.set(c.round, g)
  }

  // 确保所有已评价轮次都在 roundMap 里（即使该轮没有其他 card）
  for (const r of evaluatedRounds) {
    if (!roundMap.has(r)) roundMap.set(r, [])
  }
  // 确保当前轮次也在 roundMap 里
  if (!roundMap.has(currentRound)) roundMap.set(currentRound, [])

  const rounds: RoundGroup[] = Array.from(roundMap.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([r, cs]) => ({
      round: r,
      label: `第 ${r} 轮`,
      cards: cs,
      hasEvaluation: evaluatedRounds.has(r),
      evaluationCardIdx: evaluationCardIdxByRound.get(r) ?? cs.length,
    }))

  return { cards, rounds }
}

function StageCardRenderer({ card, input, synthLogs }: {
  card: StageCard; input: V3ExperimentInput
  synthLogs?: { time: string; level: string; msg: string }[]
}) {
  const { toolName, toolInput, toolResultData } = card
  const data = toolResultData as Record<string, unknown> | undefined

  if (toolName === 'transcribe_prompts') {
    const transcriptions = (data?.transcriptions ?? []) as {
      file_id: string; prompt_id: string; asr_text: string
      annotation?: { recording_env?: string; speaking_pace?: string; style?: string; paralanguage?: string; gender?: string; age?: string; character?: string }
    }[]
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={8}>
        {transcriptions.map((t) => (
          <Card key={t.prompt_id} size="small">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
              <Space>
                <Text strong>{t.prompt_id}</Text>
                <Text type="secondary" style={{ fontSize: 11 }}>({t.file_id.slice(0, 8)}...)</Text>
              </Space>
              {t.annotation && (
                <Space size={4}>
                  {[t.annotation.gender, t.annotation.age, t.annotation.character,
                    t.annotation.recording_env, t.annotation.speaking_pace, t.annotation.style, t.annotation.paralanguage]
                    .filter(Boolean).map((v) => (
                      <Tag key={v} style={{ fontSize: 10, margin: 0 }}>{v}</Tag>
                    ))}
                </Space>
              )}
            </div>
            <div style={{ marginTop: 4, padding: '8px 12px', background: '#fafafa', borderRadius: 4, fontSize: 13 }}>
              {t.asr_text || <Text type="secondary">（无转写结果）</Text>}
            </div>
          </Card>
        ))}
      </Space>
    )
  }

  if (toolName === 'query_kb') {
    const results = (data?.results ?? []) as { type: string; model?: string; file?: string; content: unknown }[]
    const grouped = new Map<string, typeof results>()
    for (const r of results) {
      const g = grouped.get(r.type) ?? []
      g.push(r)
      grouped.set(r.type, g)
    }
    const TYPE_LABELS: Record<string, string> = {
      profile: '模型档案', tag_tutorial: '标签教程', experience: '历史经验',
      factor: '因子库', factor_index: '因子目录', insight: '全局摘要',
      coverage: '覆盖矩阵', strategy: '历史策略',
    }
    const renderContent = (type: string, content: unknown) => {
      if (type === 'factor_index' && Array.isArray(content)) {
        return (
          <div style={{ fontSize: 11, fontFamily: 'monospace' }}>
            {(content as { factor_key?: string; confidence?: string; rule?: string }[]).map((f, i) => (
              <div key={i} style={{ marginBottom: 3, padding: '2px 0', borderBottom: '1px solid #f5f5f5' }}>
                <Tag style={{ fontSize: 10 }}>{f.confidence ?? 'low'}</Tag>
                <Text code style={{ fontSize: 10 }}>{f.factor_key}</Text>
                <span style={{ color: '#8c8c8c', marginLeft: 6 }}>{f.rule}</span>
              </div>
            ))}
          </div>
        )
      }
      if (typeof content === 'object' && content !== null) {
        return (
          <pre style={{ fontSize: 11, maxHeight: 200, overflow: 'auto', whiteSpace: 'pre-wrap', margin: 0 }}>
            {JSON.stringify(content, null, 2)}
          </pre>
        )
      }
      return (
        <div style={{ fontSize: 12, whiteSpace: 'pre-wrap', maxHeight: 150, overflow: 'auto' }}>
          {String(content ?? '')}
        </div>
      )
    }
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        {Array.from(grouped.entries()).map(([type, items]) => (
          <Card key={type} size="small" title={TYPE_LABELS[type] ?? type}>
            {items.map((item, idx) => (
              <div key={idx} style={{ marginBottom: 8, padding: '8px 12px', background: '#fafafa', borderRadius: 4 }}>
                {item.model && <Tag>{item.model}</Tag>}
                {item.file && <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>{item.file}</Text>}
                {renderContent(type, item.content)}
              </div>
            ))}
          </Card>
        ))}
        {results.length === 0 && <Text type="secondary">知识库无相关数据（冷启动）</Text>}
      </Space>
    )
  }

  if (toolName === 'design_combos') {
    const inp = toolInput as { base_texts?: string[]; combos?: { combo_id: string; type: string; model_id: string; prompt_ids: string[]; text_strategy?: string; reasoning?: string }[] } | undefined
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        {inp?.base_texts && (
          <Card size="small" title="测试文本">
            {inp.base_texts.map((t, i) => (
              <div key={i} style={{ marginBottom: 4, fontSize: 12 }}>
                <Tag>t{i}</Tag> {t}
              </div>
            ))}
          </Card>
        )}
        {inp?.combos && (
          <Card size="small" title={`组合方案（${inp.combos.length} 个）`}>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid #f0f0f0', textAlign: 'left' }}>
                    <th style={{ padding: '4px 8px' }}>ID</th>
                    <th style={{ padding: '4px 8px' }}>类型</th>
                    <th style={{ padding: '4px 8px' }}>模型</th>
                    <th style={{ padding: '4px 8px' }}>Prompt</th>
                    <th style={{ padding: '4px 8px' }}>策略/理由</th>
                  </tr>
                </thead>
                <tbody>
                  {inp.combos.map((c) => (
                    <tr key={c.combo_id} style={{ borderBottom: '1px solid #fafafa' }}>
                      <td style={{ padding: '4px 8px' }}><Text code style={{ fontSize: 11 }}>{c.combo_id}</Text></td>
                      <td style={{ padding: '4px 8px' }}>
                        <Tag color={c.type === 'exploit' ? 'green' : c.type === 'diagnostic' ? 'orange' : 'purple'}>{c.type}</Tag>
                      </td>
                      <td style={{ padding: '4px 8px' }}>{c.model_id}</td>
                      <td style={{ padding: '4px 8px' }}>{c.prompt_ids?.join(', ')}</td>
                      <td style={{ padding: '4px 8px', color: '#8c8c8c', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {c.reasoning ?? c.text_strategy ?? '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </Space>
    )
  }

  if (toolName === 'run_synthesis') {
    const result = data as { round?: number; total?: number; ok?: number; fail?: number } | undefined
    const logs = synthLogs ?? []
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        {!result && input.synthesis_progress && (
          <Card size="small" title="合成进度" style={{ borderColor: '#91caff' }}>
            <Progress
              percent={Math.round(input.synthesis_progress.done / (input.synthesis_progress.total || 1) * 100)}
              status="active"
              format={() => `${input.synthesis_progress!.done} / ${input.synthesis_progress!.total}`}
            />
            {input.current_synth_model && (
              <Text type="secondary" style={{ fontSize: 11, marginTop: 4, display: 'block' }}>
                正在合成: {input.current_synth_model}
              </Text>
            )}
          </Card>
        )}
        {result && (
          <Card size="small" title={`合成完成 — 第 ${result.round ?? '?'} 轮`}>
            <Space size={16}>
              <Text>共 <Text strong>{result.total}</Text> 条</Text>
              <Text style={{ color: '#52c41a' }}>成功 {result.ok}</Text>
              {(result.fail ?? 0) > 0 && <Text style={{ color: '#ff4d4f' }}>失败 {result.fail}</Text>}
            </Space>
          </Card>
        )}
        {logs.length > 0 && (
          <Card size="small" title="合成日志" bodyStyle={{ padding: 0 }}>
            <div style={{
              maxHeight: 220, overflowY: 'auto', padding: '6px 10px',
              fontFamily: 'monospace', fontSize: 11, lineHeight: '18px',
              background: '#1a1a2e',
            }}>
              {logs.map((l, i) => (
                <div key={i} style={{
                  color: l.level === 'error' ? '#ff6b6b' : l.msg.includes('FAIL') ? '#ffa94d' : '#a9e34b',
                  whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                }}>
                  <span style={{ color: '#666', marginRight: 6 }}>{l.time.slice(11, 19)}</span>
                  {l.msg}
                </div>
              ))}
            </div>
          </Card>
        )}
      </Space>
    )
  }

  if (toolName === 'update_portfolio') {
    const portfolio = (data as { portfolio_size?: number })?.portfolio_size
    const portfolioItems = input.portfolio ?? []
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={8}>
        <Text>当前策略数: <Text strong>{portfolio ?? portfolioItems.length}</Text></Text>
        {portfolioItems.map((p, i) => (
          <Card key={p.strategy_id || i} size="small" style={{ border: '1px solid #d9d9d9' }}>
            <Row gutter={12} align="middle">
              <Col flex="40px">
                <div style={{ fontSize: 18, fontWeight: 700, textAlign: 'center', color: i === 0 ? '#faad14' : '#8c8c8c' }}>#{i + 1}</div>
              </Col>
              <Col flex="auto">
                <Text strong>{p.strategy_id}</Text> <Tag>{p.model_id}</Tag>
                {p.vs_target_mode && <Tag color="green">{p.vs_target_mode}</Tag>}
                <div style={{ fontSize: 12, color: '#595959', marginTop: 2 }}>{p.profile}</div>
                {p.best_for && p.best_for.length > 0 && (
                  <div style={{ marginTop: 2 }}>
                    {p.best_for.map((b) => <Tag key={b} style={{ fontSize: 10 }}>{b}</Tag>)}
                  </div>
                )}
              </Col>
            </Row>
          </Card>
        ))}
      </Space>
    )
  }

  if (toolName === 'write_experience') {
    const path = (data as { path?: string })?.path
    return (
      <div>
        <Alert type="success" message="经验已写入知识库" description={path} showIcon />
      </div>
    )
  }

  if (toolName === 'update_model_knowledge') {
    const inp = toolInput as { model_id?: string; profile_finding?: string; tag_finding?: string } | undefined
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={8}>
        {inp?.model_id && <Text><Text strong>模型：</Text>{inp.model_id}</Text>}
        {inp?.profile_finding && (
          <Card size="small" title="新发现">
            <div style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>{inp.profile_finding}</div>
          </Card>
        )}
        {inp?.tag_finding && (
          <Card size="small" title="Tag 使用规律">
            <div style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>{inp.tag_finding}</div>
          </Card>
        )}
        <Alert type="success" showIcon message="模型档案已更新" />
      </Space>
    )
  }

  if (toolName === 'distill_experience') {
    const res = data as { factors_written?: number; insights_updated?: boolean; factor_keys?: string[] } | undefined
    return (
      <Space direction="vertical" style={{ width: '100%' }} size={8}>
        <Alert
          type={res?.factors_written ? 'success' : 'info'}
          showIcon
          message={res?.factors_written ? `提炼完成：${res.factors_written} 个因子` : '无新因子（当前经验已被现有知识覆盖）'}
        />
        {res?.factor_keys && res.factor_keys.length > 0 && (
          <Card size="small" title="生成的因子">
            {res.factor_keys.map((k) => <Tag key={k} style={{ marginBottom: 4 }}>{k}</Tag>)}
          </Card>
        )}
      </Space>
    )
  }

  if (toolName === 'finish_experiment') {
    return (
      <Alert type="success" showIcon message="实验完成"
        description={`原因: ${(data as { ok?: boolean })?.ok ? '成功' : '未知'}`} />
    )
  }

  // Fallback: raw JSON
  return (
    <Card size="small">
      <pre style={{ fontSize: 11, maxHeight: 300, overflow: 'auto', whiteSpace: 'pre-wrap' }}>
        {JSON.stringify(toolResultData, null, 2)}
      </pre>
    </Card>
  )
}

// ── Portfolio Panel (v3.3 success page) ─────────────────────────────────────

function PortfolioPanel({ task, input, onNewExperiment }: { task: TaskOut; input: V3ExperimentInput; onNewExperiment: () => void }) {
  const result = task.result as { reason?: string; summary?: string; portfolio?: PortfolioItem[]; total_rounds?: number } | null
  const portfolio = result?.portfolio ?? input.portfolio ?? []

  return (
    <div style={{ maxWidth: 900 }}>
      <Alert type="success" showIcon message="克隆实验完成！" description={result?.summary} style={{ marginBottom: 16 }} />

      <Card title={`策略档案（${portfolio.length} 个策略）`} style={{ marginBottom: 16 }}>
        {portfolio.length === 0 ? (
          <Text type="secondary">无策略记录</Text>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            {portfolio.map((p, i) => (
              <Card key={p.strategy_id || i} size="small" style={{ border: '1px solid #d9d9d9' }}>
                <Row align="middle" gutter={16}>
                  <Col flex="50px">
                    <div style={{ textAlign: 'center' }}>
                      <div style={{ fontSize: 20, fontWeight: 700, color: i === 0 ? '#faad14' : '#8c8c8c' }}>
                        #{i + 1}
                      </div>
                      {p.avg_rank != null && (
                        <div style={{ fontSize: 10, color: '#8c8c8c' }}>avg {p.avg_rank.toFixed(1)}</div>
                      )}
                    </div>
                  </Col>
                  <Col flex="auto">
                    <div>
                      <Text strong>{p.strategy_id}</Text>
                      <Tag style={{ marginLeft: 8 }}>{p.model_id}</Tag>
                      {p.vs_target_mode && <Tag color="green">{p.vs_target_mode}</Tag>}
                    </div>
                    <div style={{ marginTop: 4, color: '#595959', fontSize: 12 }}>{p.profile}</div>
                    {p.best_for && p.best_for.length > 0 && (
                      <div style={{ marginTop: 4 }}>
                        <Text type="secondary" style={{ fontSize: 11 }}>适用：</Text>
                        {p.best_for.map((b) => <Tag key={b} style={{ fontSize: 11 }}>{b}</Tag>)}
                      </div>
                    )}
                  </Col>
                </Row>
              </Card>
            ))}
          </Space>
        )}
      </Card>

      <Card title="实验信息" size="small">
        <p><Text strong>总轮次：</Text>{result?.total_rounds ?? input.round ?? '-'}</p>
        <p><Text strong>结束原因：</Text>{result?.reason ?? '-'}</p>
        {input.experience_file_path && <p><Text strong>经验文件：</Text>{input.experience_file_path}</p>}
      </Card>

      <Space style={{ marginTop: 16 }}>
        <Button type="primary" icon={<RocketOutlined />} onClick={() => message.info('策略导出 → 批量合成（开发中）')}>
          导出策略 → 批量合成
        </Button>
        <Button onClick={onNewExperiment}>新建实验</Button>
      </Space>
    </div>
  )
}

// ── Agent Running Panel (main area — page-by-page navigation) ────────────────

function AgentRunningPanel({ task, input, taskId, qc, onNewExperiment, synthLogs, models }: {
  task: TaskOut; input: V3ExperimentInput; taskId: string;
  qc: ReturnType<typeof useQueryClient>; onNewExperiment: () => void
  synthLogs: { time: string; level: string; msg: string }[]
  models: ModelInfo[]
}) {
  const events = input.agent_events ?? []
  const { rounds } = useMemo(() => buildStageCards(events), [events])
  const status = task.status
  const phase = input.phase

  // Build virtual pages: each round's cards + special pages (evaluation, portfolio)
  type PageItem = { type: 'card'; card: StageCard } | { type: 'evaluation' } | { type: 'portfolio' }
  type RoundPage = { round: number; label: string; pages: PageItem[] }

  const roundPages: RoundPage[] = useMemo(() => {
    const result: RoundPage[] = rounds.map((r) => {
      const allCards: PageItem[] = r.cards.map((card) => ({ type: 'card' as const, card }))

      if (r.hasEvaluation) {
        // 把 evaluation page 插到正确位置：synthesis 之后、post-eval 分析之前
        // evaluationCardIdx = request_user_review 触发时该轮已有的卡片数
        const idx = Math.min(r.evaluationCardIdx, allCards.length)
        const pages: PageItem[] = [
          ...allCards.slice(0, idx),
          { type: 'evaluation' },
          ...allCards.slice(idx),
        ]
        return { round: r.round, label: r.label, pages }
      }

      return { round: r.round, label: r.label, pages: allCards }
    })

    // 当前轮正在等待评价：末尾轮加 evaluation page（如果还没加过）
    if (status === 'awaiting_review' || phase === 'awaiting_review') {
      if (result.length > 0) {
        const last = result[result.length - 1]
        if (!last.pages.some((p) => p.type === 'evaluation')) {
          last.pages.push({ type: 'evaluation' })
        }
      } else {
        result.push({ round: 1, label: '第 1 轮', pages: [{ type: 'evaluation' }] })
      }
    }

    // Add portfolio page to the latest round if experiment completed
    if (status === 'success' || phase === 'success') {
      if (result.length > 0) {
        result[result.length - 1].pages.push({ type: 'portfolio' })
      } else {
        result.push({ round: 1, label: '第 1 轮', pages: [{ type: 'portfolio' }] })
      }
    }

    return result
  }, [rounds, status, phase])

  const [activeRound, setActiveRound] = useState<number | null>(null)
  const [pageIdx, setPageIdx] = useState<number | null>(null)
  const [userNavigated, setUserNavigated] = useState(false)

  // Current round index (null = follow latest)
  const currentRoundIdx = activeRound ?? roundPages.length - 1
  const currentRound = roundPages[currentRoundIdx]
  const pages = currentRound?.pages ?? []

  // Auto-follow: 只有在用户没有手动导航、且是最新轮时，才跟随最新 page
  // 不在用户手动浏览历史轮时触发
  useEffect(() => {
    if (userNavigated) return
    if (activeRound !== null) return  // 用户切到了历史轮，不干扰
    if (pages.length > 0) {
      setPageIdx(pages.length - 1)
    }
  }, [pages.length, activeRound, userNavigated])

  // 轮次总数变化时（新一轮开始），自动切到最新轮
  const prevRoundCount = useRef(roundPages.length)
  useEffect(() => {
    if (roundPages.length > prevRoundCount.current) {
      // 新轮开始，切到最新轮，显示最后一页
      setActiveRound(null)
      setPageIdx(null)
      setUserNavigated(false)
    }
    prevRoundCount.current = roundPages.length
  }, [roundPages.length])

  // When user explicitly switches round, reset to latest page of that round
  useEffect(() => {
    if (activeRound !== null) {
      const r = roundPages[activeRound]
      if (r) setPageIdx(r.pages.length - 1)
    }
  }, [activeRound])  // 只在 activeRound 变化时触发，不依赖 roundPages

  const currentPage = pageIdx !== null ? Math.min(pageIdx, pages.length - 1) : pages.length - 1
  const page = pages[currentPage]

  const progress = input.synthesis_progress
  const isRunning = status === 'running'

  const lastEvent = events[events.length - 1]
  const isAnalyzing = isRunning && lastEvent && (
    lastEvent.type === 'thinking' ||
    (lastEvent.type === 'tool_result' && lastEvent.tool_name === 'request_user_review')
  )

  function goPage(idx: number) {
    setPageIdx(idx)
    setUserNavigated(true)
  }

  function goLatest() {
    setActiveRound(null)
    setPageIdx(null)
    setUserNavigated(false)
  }

  function switchRound(idx: number) {
    setActiveRound(idx === roundPages.length - 1 ? null : idx)
    setUserNavigated(idx !== roundPages.length - 1)
  }

  return (
    <div style={{ padding: 24, flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      {/* Progress bar during synthesis */}
      {isRunning && progress && progress.total > 0 && progress.done < progress.total && (
        <Card size="small" style={{ marginBottom: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <LoadingOutlined style={{ color: '#1890ff' }} />
            <div style={{ flex: 1 }}>
              <div style={{ height: 6, background: '#f0f0f0', borderRadius: 3 }}>
                <div style={{ height: 6, borderRadius: 3, background: '#1890ff', width: `${(progress.done / progress.total) * 100}%`, transition: 'width 0.3s' }} />
              </div>
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>{progress.done}/{progress.total}</Text>
          </div>
        </Card>
      )}

      {/* Round tabs */}
      {roundPages.length > 1 && (
        <div style={{ marginBottom: 8, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          {roundPages.map((r, i) => (
            <Button key={r.round} size="small"
              type={i === currentRoundIdx ? 'primary' : 'default'}
              onClick={() => switchRound(i)}>
              {r.label}
            </Button>
          ))}
        </div>
      )}

      {/* Page navigation within current round */}
      {pages.length > 0 && (
        <div style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'space-between' }}>
          <Button size="small" disabled={currentPage <= 0}
            onClick={() => goPage(currentPage - 1)}>
            ←
          </Button>
          <Space size={4}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {currentPage + 1} / {pages.length}
            </Text>
            {page?.type === 'card' && (
              <Tag style={{ fontSize: 11 }}>{page.card.icon} {page.card.title}</Tag>
            )}
            {page?.type === 'evaluation' && <Tag color="orange" style={{ fontSize: 11 }}>⏸ 评价</Tag>}
            {page?.type === 'portfolio' && <Tag color="green" style={{ fontSize: 11 }}>🏁 策略档案</Tag>}
          </Space>
          <Space size={4}>
            <Button size="small" disabled={currentPage >= pages.length - 1}
              onClick={() => goPage(currentPage + 1)}>
              →
            </Button>
            {userNavigated && (
              <Button size="small" type="link" onClick={goLatest}>
                最新
              </Button>
            )}
          </Space>
        </div>
      )}

      {/* Page content */}
      <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
        {!page ? (
          <div style={{ textAlign: 'center', padding: 48 }}>
            {isAnalyzing ? (
              <>
                <LoadingOutlined style={{ fontSize: 24, color: '#722ed1' }} />
                <div style={{ marginTop: 16 }}>
                  <Title level={5} style={{ color: '#722ed1' }}>Agent 正在分析…</Title>
                  <Text type="secondary">等待结果输出</Text>
                </div>
              </>
            ) : (
              <>
                <Spin size="large" />
                <div style={{ marginTop: 16 }}>
                  <Title level={5}>Agent 正在调用模型…</Title>
                  <Text type="secondary">思考过程在左侧实时显示</Text>
                </div>
              </>
            )}
          </div>
        ) : page.type === 'card' ? (
          <Card title={<span>{page.card.icon} {page.card.title}</span>}
            size="small" style={{ borderColor: '#f0f0f0' }}
            extra={<Text type="secondary" style={{ fontSize: 11 }}>{page.card.ts?.slice(11, 19)}</Text>}>
            <StageCardRenderer card={page.card} input={input} synthLogs={synthLogs} />
          </Card>
        ) : page.type === 'evaluation' ? (
          <EvaluationPanel input={input} taskId={taskId} qc={qc} models={models} />
        ) : page.type === 'portfolio' ? (
          <PortfolioPanel task={task} input={input} onNewExperiment={onNewExperiment} />
        ) : null}
      </div>
    </div>
  )
}

// ── evaluation panel ──────────────────────────────────────────────────────────

function usePersistedState<T>(key: string, initialValue: T): [T, (v: T | ((prev: T) => T)) => void] {
  const [state, _setState] = useState<T>(() => {
    try {
      const stored = sessionStorage.getItem(key)
      return stored ? JSON.parse(stored) : initialValue
    } catch { return initialValue }
  })
  const setState = useCallback((v: T | ((prev: T) => T)) => {
    _setState((prev) => {
      const next = typeof v === 'function' ? (v as (p: T) => T)(prev) : v
      try { sessionStorage.setItem(key, JSON.stringify(next)) } catch { /* quota */ }
      return next
    })
  }, [key])
  return [state, setState]
}

function SortableComboCard({ id, children }: { id: string; children: React.ReactNode }) {
  const { attributes, listeners, setNodeRef, transform, transition } = useSortable({ id })
  const style = { transform: CSS.Transform.toString(transform), transition }
  return (
    <div ref={setNodeRef} style={style}>
      <Row align="middle" gutter={12}>
        <Col flex="30px">
          <div {...attributes} {...listeners} style={{ cursor: 'grab', textAlign: 'center', padding: '8px 0' }}>
            <HolderOutlined style={{ fontSize: 16, color: '#999' }} />
          </div>
        </Col>
        <Col flex="auto">{children}</Col>
      </Row>
    </div>
  )
}

function EvaluationPanel({ input, taskId, qc, models }: { input: V3ExperimentInput; taskId: string; qc: ReturnType<typeof useQueryClient>; models: ModelInfo[] }) {
  const results = (input.synthesis_results ?? []).filter((r) => r.status === 'success')
  const round = input.round
  const storagePrefix = `eval_v3_${taskId}_r${round}`

  const resultsKey = results.map((r) => r.combo_id + r.text_idx).join(',')
  const textGroups = useMemo(() => {
    const groups = new Map<number, V3SynthesisResult[]>()
    for (const r of results) {
      const g = groups.get(r.text_idx) ?? []
      g.push(r)
      groups.set(r.text_idx, g)
    }
    for (const [, g] of groups) {
      for (let i = g.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [g[i], g[j]] = [g[j], g[i]]
      }
    }
    return groups
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resultsKey])

  const [rankings, setRankings] = usePersistedState<Record<string, { combo_id: string; rank: number }[]>>(`${storagePrefix}_rank`, {})
  const [perCombo, setPerCombo] = usePersistedState<Record<string, { vs_target?: string; issues?: string[]; notes?: string }>>(`${storagePrefix}_eval`, {})
  const [winner, setWinner] = usePersistedState<string | null>(`${storagePrefix}_winner`, null)
  const [nextDir, setNextDir] = usePersistedState<string>(`${storagePrefix}_dir`, '')
  const [loading, setLoading] = useState(false)

  // ── 构建 prompt 音频映射（从 transcribe_prompts 事件中提取）──
  const promptAudioMap = useMemo(() => {
    const map: Record<string, { audioUrl: string; asrText: string }> = {}
    const events = input.agent_events ?? []
    for (const ev of events) {
      if (ev.type === 'tool_result' && ev.tool_name === 'transcribe_prompts' && ev.tool_result_data) {
        const transcriptions = (ev.tool_result_data as Record<string, unknown>)?.transcriptions as
          { file_id?: string; prompt_id?: string; asr_text?: string }[] | undefined
        if (Array.isArray(transcriptions)) {
          for (const t of transcriptions) {
            if (t.prompt_id && t.file_id) {
              map[t.prompt_id] = { audioUrl: `/api/files/${t.file_id}/download`, asrText: t.asr_text ?? '' }
            }
          }
        }
        break  // 只取第一个 transcribe_prompts 结果
      }
    }
    return map
  }, [input.agent_events])

  // 模型 ID → display_name 映射
  const modelDisplayName = useMemo(() => {
    const m: Record<string, string> = {}
    for (const model of models) m[model.id] = model.display_name
    return m
  }, [models])

  // ── 当前评价轮的 base_texts（从该轮 design_combos 事件中提取，而非 current_round_plan）──
  const roundBaseTexts = useMemo(() => {
    const events = input.agent_events ?? []
    // 找到当前评价轮对应的 design_combos tool_call（倒序找最近的，且 round 匹配）
    for (let i = events.length - 1; i >= 0; i--) {
      const ev = events[i]
      if (ev.type === 'tool_call' && ev.tool_name === 'design_combos' && ev.tool_input) {
        const inp = ev.tool_input as { base_texts?: string[]; round?: number } | undefined
        if (inp?.base_texts && (inp.round === round || inp.round === undefined)) {
          return inp.base_texts
        }
      }
    }
    // fallback: current_round_plan
    const plan = input.current_round_plan as Record<string, unknown> | undefined
    return (plan?.base_texts as string[] | undefined) ?? null
  }, [input.agent_events, input.current_round_plan, round])

  // Initialize rankings from group order only if no persisted data
  useEffect(() => {
    if (Object.keys(rankings).length > 0) return
    const init: Record<string, { combo_id: string; rank: number }[]> = {}
    for (const [tIdx, group] of textGroups) {
      init[String(tIdx)] = group.map((r, i) => ({ combo_id: r.combo_id, rank: i + 1 }))
    }
    setRankings(init)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [textGroups])

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  // ── 自动排序：全部评完且排名与评分不一致时自动触发 ──
  const autoSortRef = useRef(false)
  useEffect(() => {
    if (autoSortRef.current) {
      autoSortRef.current = false
      return  // 本次 rankings 变化是由 autoSort 引起的，跳过
    }
    for (const [tIdx, group] of textGroups) {
      const textIdxStr = String(tIdx)
      const ranked = rankings[textIdxStr]
      if (!ranked || ranked.length === 0) continue

      // 检查是否所有 combo 都已评分
      const allRated = ranked.every((item) => {
        const ek = `${item.combo_id}_t${textIdxStr}`
        return !!perCombo[ek]?.vs_target
      })
      if (!allRated) continue

      // 计算按评分排序的预期顺序
      const scoreOf = (comboId: string) => {
        const ek = `${comboId}_t${textIdxStr}`
        const vt = perCombo[ek]?.vs_target ?? ''
        return parseInt(vt.split('_')[0]) || 0
      }
      const expected = [...ranked].sort((a, b) => scoreOf(b.combo_id) - scoreOf(a.combo_id))
      const isSame = ranked.every((item, idx) => item.combo_id === expected[idx].combo_id)
      if (!isSame) {
        autoSortRef.current = true
        autoSort(textIdxStr)
      }
    }
  }, [perCombo, rankings, textGroups])

  function handleDragEnd(textIdx: string, event: DragEndEvent) {
    const { active, over } = event
    if (!over || active.id === over.id) return
    setRankings((prev) => {
      const list = [...(prev[textIdx] ?? [])]
      const oldIndex = list.findIndex((r) => r.combo_id === active.id)
      const newIndex = list.findIndex((r) => r.combo_id === over.id)
      if (oldIndex < 0 || newIndex < 0) return prev
      const reordered = arrayMove(list, oldIndex, newIndex)
      return { ...prev, [textIdx]: reordered.map((r, i) => ({ ...r, rank: i + 1 })) }
    })
  }

  function autoSort(textIdx: string) {
    setRankings((prev) => {
      const list = [...(prev[textIdx] ?? [])]
      const scoreMap: Record<string, number> = {}
      for (const item of list) {
        const ek = `${item.combo_id}_t${textIdx}`
        const vt = perCombo[ek]?.vs_target ?? ''
        const num = parseInt(vt.split('_')[0]) || 0
        scoreMap[item.combo_id] = num
      }
      list.sort((a, b) => (scoreMap[b.combo_id] || 0) - (scoreMap[a.combo_id] || 0))
      return { ...prev, [textIdx]: list.map((r, i) => ({ ...r, rank: i + 1 })) }
    })
  }

  async function submit(finishRequested = false) {
    setLoading(true)
    try {
      await api.submitEvaluationV3(taskId, {
        round,
        rankings_by_text: rankings,
        per_combo: perCombo,
        winner,
        next_round_direction: nextDir,
        finish_requested: finishRequested,
      })
      // Clear persisted state on success
      sessionStorage.removeItem(`${storagePrefix}_rank`)
      sessionStorage.removeItem(`${storagePrefix}_eval`)
      sessionStorage.removeItem(`${storagePrefix}_winner`)
      sessionStorage.removeItem(`${storagePrefix}_dir`)
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? ''
      if (!msg.includes('status=')) message.error('提交失败')
    } finally {
      qc.invalidateQueries({ queryKey: ['experiment-v3', taskId] })
      setLoading(false)
    }
  }

  return (
    <div style={{ maxWidth: 1000 }}>
      <Title level={5}>第 {round} 轮评价</Title>
      <Text type="secondary" style={{ display: 'block', marginBottom: 16, fontSize: 12 }}>
        按文本分组，拖拽卡片调整排名（从最好到最差）。
      </Text>

      {/* Prompt 音频参考区 */}
      {Object.keys(promptAudioMap).length > 0 && (
        <Card size="small" title="Prompt 音频参考" style={{ marginBottom: 16 }}
          extra={<Text type="secondary" style={{ fontSize: 11 }}>点击播放对照原始 prompt</Text>}>
          <Space direction="vertical" style={{ width: '100%' }} size={6}>
            {Object.entries(promptAudioMap).map(([pid, info]) => (
              <div key={pid} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Tag color="green">{pid}</Tag>
                <AudioPlayer url={info.audioUrl} />
                {info.asrText && (
                  <Text type="secondary" style={{ fontSize: 11, maxWidth: 400, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {info.asrText}
                  </Text>
                )}
              </div>
            ))}
          </Space>
        </Card>
      )}

      <Space direction="vertical" style={{ width: '100%' }} size={16}>
        {Array.from(textGroups.entries()).sort(([a], [b]) => a - b).map(([tIdx, group]) => {
          const textIdxStr = String(tIdx)
          const ranked = rankings[textIdxStr] ?? group.map((r, i) => ({ combo_id: r.combo_id, rank: i + 1 }))
          const orderedResults = ranked.map((r) => group.find((g) => g.combo_id === r.combo_id)!).filter(Boolean)

          // 组标题文本：优先用该轮 base_texts 原文（未标签化），否则 fallback 到第一个结果的 text
          const baseText = roundBaseTexts?.[tIdx]
          const groupTitleText = baseText ?? group[0]?.text ?? ''

          return (
            <Card key={tIdx} size="small"
              title={`文本 ${tIdx + 1}: "${groupTitleText}"`}
              extra={<Button size="small" icon={<SortAscendingOutlined />} onClick={() => autoSort(textIdxStr)}>按评分排序</Button>}>
              <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={(e) => handleDragEnd(textIdxStr, e)}>
                <SortableContext items={orderedResults.map((r) => r.combo_id)} strategy={verticalListSortingStrategy}>
                  <Space direction="vertical" style={{ width: '100%' }} size={8}>
                    {orderedResults.map((sr, i) => {
                      const ek = `${sr.combo_id}_t${tIdx}`
                      const ev = perCombo[ek] ?? {}
                      return (
                        <SortableComboCard key={sr.combo_id} id={sr.combo_id}>
                          <Card size="small"
                            style={{ border: winner === sr.audio_label ? '2px solid #1890ff' : '1px solid #f0f0f0' }}>
                            <Row align="middle" gutter={8}>
                              <Col flex="30px">
                                <Text strong style={{ display: 'block', textAlign: 'center', fontSize: 16 }}>{i + 1}</Text>
                              </Col>
                              <Col flex="auto">
                                <Space>
                                  <Text code style={{ fontSize: 11 }}>{sr.audio_label}</Text>
                                  <Tag color="blue" style={{ fontSize: 11 }}>{modelDisplayName[sr.model_id] ?? sr.model_id}</Tag>
                                  {sr.prompt_ids.map((pid) => <Tag key={pid} color="green" style={{ fontSize: 11 }}>{pid}</Tag>)}
                                  <AudioPlayer url={sr.audio_url!} />
                                </Space>
                                {/* 实际合成文本：和 base_text 不同时高亮提示 */}
                                {sr.text && sr.text !== groupTitleText && (
                                  <div style={{ marginTop: 2, padding: '2px 6px', background: '#fff7e6', borderRadius: 4, borderLeft: '2px solid #faad14', fontSize: 11 }}>
                                    <Text type="secondary" style={{ fontSize: 10 }}>实际文本：</Text>
                                    <Text style={{ fontSize: 11 }}>{sr.text}</Text>
                                  </div>
                                )}
                                <div style={{ marginTop: 8 }}>
                                  <Radio.Group size="small" value={ev.vs_target}
                                    onChange={(e) => setPerCombo((prev) => ({ ...prev, [ek]: { ...ev, vs_target: e.target.value } }))}>
                                    {VS_TARGET_OPTIONS.map((o) => <Radio.Button key={o.value} value={o.value}>{o.label}</Radio.Button>)}
                                  </Radio.Group>
                                </div>
                                <div style={{ marginTop: 4 }}>
                                  <Checkbox.Group options={ISSUE_TAGS} value={ev.issues ?? []}
                                    onChange={(v) => setPerCombo((prev) => ({ ...prev, [ek]: { ...ev, issues: v as string[] } }))}
                                    style={{ fontSize: 11 }} />
                                </div>
                                <Input size="small" placeholder="备注" value={ev.notes ?? ''}
                                  onChange={(e) => setPerCombo((prev) => ({ ...prev, [ek]: { ...ev, notes: e.target.value } }))}
                                  style={{ marginTop: 4, width: 300 }} />
                              </Col>
                            </Row>
                          </Card>
                        </SortableComboCard>
                      )
                    })}
                  </Space>
                </SortableContext>
              </DndContext>
            </Card>
          )
        })}
      </Space>

      <Card style={{ marginTop: 16 }}>
        <Space direction="vertical" style={{ width: '100%' }}>
          {input._review_message && (
            <Alert type="info" message={input._review_message} style={{ marginBottom: 8 }} />
          )}
          <div>
            <Text>反馈给 Agent（可选）：</Text>
            <Input value={nextDir} onChange={(e) => setNextDir(e.target.value)} placeholder="如：希望情感更强烈 / 换一个 prompt 试试" />
          </div>
          <Space>
            <Button type="primary" size="large" loading={loading} onClick={() => submit(false)}>
              提交评价
            </Button>
            <Button size="large" loading={loading} onClick={() => submit(true)}>
              提交并结束实验
            </Button>
          </Space>
        </Space>
      </Card>
    </div>
  )
}
