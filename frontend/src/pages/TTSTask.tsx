import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Alert, Button, Card, Checkbox, Col, Modal, Progress, Row, Space,
  Table, Tag, Typography, Upload, message,
} from 'antd'
import { SettingOutlined, UploadOutlined } from '@ant-design/icons'
import { api, FileOut, ModelInfo, ParamSchema, PromptInfo, TaskOut } from '../api/client'
import TaskStatusTag from '../components/TaskStatusTag'
import AudioPlayer from '../components/AudioPlayer'
import ModelParamsForm from '../components/ModelParamsForm'

const { Title, Text } = Typography

interface ResultItem {
  key: string
  status: string
  audio_url?: string
  error?: string
}

function initParams(schema: Record<string, ParamSchema>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(schema).map(([k, s]) => [k, s.default]))
}

export default function TTSTask() {
  const navigate = useNavigate()

  const { data: models = [] } = useQuery<ModelInfo[]>({
    queryKey: ['models'],
    queryFn: api.getModels,
  })

  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [settingModelId, setSettingModelId] = useState<string | null>(null)
  const [jsonlFile, setJsonlFile] = useState<FileOut | null>(null)
  const [promptFile, setPromptFile] = useState<FileOut | null>(null)
  const [paramsMap, setParamsMap] = useState<Record<string, Record<string, unknown>>>({})
  const [promptMap, setPromptMap] = useState<Record<string, string>>({})
  const [analyzing, setAnalyzing] = useState(false)
  const [analyzeResult, setAnalyzeResult] = useState<{
    total_items: number; prompts: PromptInfo[]; missing_prompts: string[]
    has_any_prompt: boolean; empty_source_path_count: number
  } | null>(null)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const isBenchmark = selectedIds.length > 1

  useEffect(() => {
    setParamsMap((prev) => {
      const next = { ...prev }
      for (const id of selectedIds) {
        if (!next[id]) {
          const m = models.find((m) => m.id === id)
          next[id] = m ? initParams(m.params_schema.properties ?? {}) : {}
        }
      }
      return next
    })
  }, [selectedIds, models])

  const uploadJsonl = async (file: File) => {
    try {
      const out = await api.uploadFile(file)
      setJsonlFile(out)
      setAnalyzeResult(null)
      setPromptMap({})
      setPromptFile(null)
      message.success(`已上传: ${out.original_name}`)
      setAnalyzing(true)
      try {
        const res = await api.analyzeJsonl(out.file_id)
        setAnalyzeResult(res)
      } catch {
        message.error('JSONL 分析失败')
      } finally {
        setAnalyzing(false)
      }
    } catch {
      message.error('上传失败')
    }
    return false
  }

  const uploadPrompt = async (file: File) => {
    try {
      const out = await api.uploadFile(file)
      setPromptFile(out)
      message.success(`已上传: ${out.original_name}`)
    } catch {
      message.error('上传失败')
    }
    return false
  }

  const uploadMissingPrompt = async (file: File, basename: string) => {
    try {
      const out = await api.uploadFile(file)
      setPromptMap((prev) => ({ ...prev, [basename]: out.file_id }))
      message.success(basename ? `已映射: ${basename}` : `已映射空路径条目`)
    } catch {
      message.error('上传失败')
    }
    return false
  }

  const handleSubmit = async () => {
    if (!jsonlFile) return message.warning('请先上传 JSONL 文件')
    if (selectedIds.length === 0) return message.warning('请选择至少一个模型')
    if (analyzeResult && !promptFile) {
      // 无全局 Prompt 时：空路径条目和缺失文件条目都必须有映射
      if (analyzeResult.empty_source_path_count > 0 && !promptMap['']) {
        return message.warning(`有 ${analyzeResult.empty_source_path_count} 条 source_path 为空，请在映射卡片中上传对应 Prompt 或上传全局 Prompt`)
      }
      const stillMissing = analyzeResult.missing_prompts.filter((bn) => !promptMap[bn])
      if (stillMissing.length > 0) {
        return message.warning(`还有 ${stillMissing.length} 个 Prompt 未上传：${stillMissing.join(', ')}`)
      }
    }
    setSubmitting(true)
    try {
      if (isBenchmark) {
        const { benchmark_run_id } = await api.runBenchmark({
          jsonl_file_id: jsonlFile.file_id,
          model_ids: selectedIds,
          prompt_audio_file_id: promptFile?.file_id,
          prompt_map: promptMap,
          params_per_model: paramsMap,
        })
        navigate(`/benchmark/${benchmark_run_id}`)
      } else {
        const modelId = selectedIds[0]
        const { task_id } = await api.runTTS(
          jsonlFile.file_id,
          modelId,
          promptFile?.file_id,
          promptMap,
          paramsMap[modelId] ?? {},
        )
        setTaskId(task_id)
      }
    } catch {
      message.error('创建任务失败')
    }
    setSubmitting(false)
  }

  const { data: task } = useQuery<TaskOut>({
    queryKey: ['task', taskId],
    queryFn: () => api.getTask(taskId!),
    enabled: !!taskId,
    refetchInterval: (q) => {
      const s = (q.state.data as TaskOut | undefined)?.status
      return s === 'running' || s === 'pending' ? 2000 : false
    },
  })

  const result = task?.result as { items: ResultItem[]; total: number; ok: number; fail: number } | null
  const logs = task?.logs ?? []
  const lastLog = logs[logs.length - 1]?.msg ?? ''
  const progressPct = result
    ? Math.round(((result.ok ?? 0) + (result.fail ?? 0)) / (result.total || 1) * 100)
    : undefined

  const resultColumns = [
    { title: 'Key', dataIndex: 'key', key: 'key', ellipsis: true },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 80,
      render: (v: string) => <TaskStatusTag status={v} />,
    },
    {
      title: '音频', key: 'action', width: 80,
      render: (_: unknown, row: ResultItem) =>
        row.audio_url ? <AudioPlayer url={row.audio_url} /> : <span style={{ color: '#aaa' }}>—</span>,
    },
  ]

  const emptyPathCount = analyzeResult?.empty_source_path_count ?? 0
  const missingFiles = analyzeResult?.prompts.filter((p) => !p.exists_on_server) ?? []
  const needsMappingCard = analyzeResult !== null && (emptyPathCount > 0 || missingFiles.length > 0)

  return (
    <>
      <Title level={4} style={{ marginTop: 0 }}>推理任务</Title>

      {/* 模型选择 */}
      <Card
        title={
          <Space>
            选择模型
            {isBenchmark && <Tag color="blue">Benchmark 模式</Tag>}
          </Space>
        }
        style={{ marginBottom: 16 }}
      >
        <Row gutter={[12, 8]}>
            {[...models].sort((a, b) => (a.model_type === 'api' ? 0 : 1) - (b.model_type === 'api' ? 0 : 1)).map((m) => {
              const isLocal = m.model_type !== 'api'
              const hasParams = Object.keys(m.params_schema.properties ?? {}).length > 0
              const isSelected = selectedIds.includes(m.id)
              return (
                <Col key={m.id} xs={12} sm={8} md={6}>
                  <Space size={4}>
                    <Checkbox
                      value={m.id}
                      checked={isSelected}
                      disabled={isLocal}
                      onChange={(e) => {
                        if (e.target.checked) setSelectedIds((p) => [...p, m.id])
                        else setSelectedIds((p) => p.filter((id) => id !== m.id))
                      }}
                    >
                      <span style={{ color: isLocal ? '#aaa' : undefined }}>{m.display_name}</span>
                    </Checkbox>
                    {hasParams && (
                      <Button
                        type="text"
                        size="small"
                        icon={<SettingOutlined />}
                        onClick={() => setSettingModelId(m.id)}
                        style={{ color: isSelected ? '#1677ff' : '#aaa', padding: '0 2px' }}
                      />
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

      {/* JSONL 上传 */}
      <Card title="输入数据" style={{ marginBottom: 16 }}>
        <Space direction="vertical">
          <Upload beforeUpload={uploadJsonl} showUploadList={false} accept=".jsonl">
            <Button icon={<UploadOutlined />} loading={analyzing}>上传 JSONL</Button>
          </Upload>
          {jsonlFile && (
            <Text type="success">
              {jsonlFile.original_name}
              {analyzeResult && ` — ${analyzeResult.total_items} 条`}
            </Text>
          )}
        </Space>
      </Card>

      {/* 全局 Prompt（可选，上传后无条件覆盖所有条目） */}
      {analyzeResult !== null && (
        <Card title="全局 Prompt（可选）" style={{ marginBottom: 16 }}>
          <Space direction="vertical">
            <Text type="secondary">
              上传后覆盖所有条目的 source_path，包括已有服务器文件的条目
            </Text>
            <Space>
              <Upload beforeUpload={uploadPrompt} showUploadList={false} accept=".wav,.mp3,.flac">
                <Button icon={<UploadOutlined />}>上传 Prompt 音频</Button>
              </Upload>
              {promptFile
                ? <Tag color="success" closable onClose={() => setPromptFile(null)}>{promptFile.original_name}</Tag>
                : <Tag>未上传</Tag>}
            </Space>
          </Space>
        </Card>
      )}

      {/* Prompt 映射（空路径 + 服务器上不存在的文件，全局 Prompt 存在时自动覆盖） */}
      {needsMappingCard && (
        <Card
          title={
            <Space>
              Prompt 映射
              {promptFile
                ? <Tag color="blue">全局 Prompt 已覆盖，无需映射</Tag>
                : <Tag color="red">必填</Tag>}
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          <Space direction="vertical" style={{ width: '100%' }}>
            {/* 空路径条目（共用一个映射） */}
            {emptyPathCount > 0 && (
              <Row align="middle" gutter={12}>
                <Col flex="auto">
                  <Text type="warning">空路径条目</Text>
                  <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                    共 {emptyPathCount} 条，source_path 为空
                  </Text>
                </Col>
                <Col>
                  {promptFile
                    ? <Tag color="blue">全局覆盖</Tag>
                    : promptMap['']
                      ? <Tag color="success">已上传</Tag>
                      : (
                        <Upload
                          beforeUpload={(f) => uploadMissingPrompt(f, '')}
                          showUploadList={false}
                          accept=".wav,.mp3,.flac"
                        >
                          <Button size="small" icon={<UploadOutlined />}>上传</Button>
                        </Upload>
                      )}
                </Col>
              </Row>
            )}
            {/* 服务器上不存在的文件 */}
            {missingFiles.map((p) => (
              <Row key={p.basename} align="middle" gutter={12}>
                <Col flex="auto">
                  <Text code>{p.basename}</Text>
                  <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>{p.source_path}</Text>
                </Col>
                <Col>
                  {promptFile
                    ? <Tag color="blue">全局覆盖</Tag>
                    : promptMap[p.basename]
                      ? <Tag color="success">已上传</Tag>
                      : (
                        <Upload
                          beforeUpload={(f) => uploadMissingPrompt(f, p.basename)}
                          showUploadList={false}
                          accept=".wav,.mp3,.flac"
                        >
                          <Button size="small" icon={<UploadOutlined />}>上传</Button>
                        </Upload>
                      )}
                </Col>
              </Row>
            ))}
          </Space>
        </Card>
      )}

      {/* 参数配置 Modal */}
      {settingModelId && (() => {
        const m = models.find((m) => m.id === settingModelId)
        const schema = m?.params_schema.properties ?? {}
        return (
          <Modal
            title={`${m?.display_name ?? settingModelId} 参数`}
            open
            onCancel={() => setSettingModelId(null)}
            footer={<Button type="primary" onClick={() => setSettingModelId(null)}>确定</Button>}
          >
            {Object.keys(schema).length > 0
              ? (
                <ModelParamsForm
                  schema={schema}
                  values={paramsMap[settingModelId] ?? {}}
                  onChange={(k, v) =>
                    setParamsMap((prev) => ({
                      ...prev,
                      [settingModelId]: { ...(prev[settingModelId] ?? {}), [k]: v },
                    }))
                  }
                />
              )
              : <Text type="secondary">该模型无可配置参数</Text>}
          </Modal>
        )
      })()}

      <Button
        type="primary"
        size="large"
        onClick={handleSubmit}
        loading={submitting}
        disabled={!jsonlFile || selectedIds.length === 0}
      >
        {isBenchmark ? `提交 Benchmark (${selectedIds.length} 个模型)` : 'Generate ▶'}
      </Button>

      {/* 单任务结果 */}
      {taskId && task && (
        <Card style={{ marginTop: 24 }}>
          <Space style={{ marginBottom: 12 }}>
            <Text type="secondary">任务 ID: {taskId}</Text>
            <TaskStatusTag status={task.status} />
          </Space>
          {(task.status === 'running' || task.status === 'pending') && (
            <Progress percent={progressPct} status="active" style={{ marginBottom: 8 }} />
          )}
          {lastLog && <div style={{ color: '#888', marginBottom: 12, fontSize: 12 }}>{lastLog}</div>}
          {result && (
            <Table
              size="small"
              dataSource={result.items}
              columns={resultColumns}
              rowKey="key"
              pagination={{ pageSize: 20 }}
              summary={() => (
                <Table.Summary.Row>
                  <Table.Summary.Cell index={0} colSpan={3}>
                    <Text>共 {result.total} 条 · 成功 {result.ok} · 失败 {result.fail}</Text>
                  </Table.Summary.Cell>
                </Table.Summary.Row>
              )}
            />
          )}
          {task.error && <Alert type="error" message={task.error} style={{ marginTop: 8 }} />}
        </Card>
      )}
    </>
  )
}
