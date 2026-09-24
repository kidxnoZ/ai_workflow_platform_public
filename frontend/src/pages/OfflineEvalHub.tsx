import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Table, Button, Tag, Progress, Tabs, Form, Input, Upload, Checkbox,
  Radio, Space, message, Typography, Popconfirm, Spin, Alert,
} from 'antd'
import {
  InboxOutlined, PlusOutlined, PlayCircleOutlined,
  BarChartOutlined, DeleteOutlined,
} from '@ant-design/icons'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import type { UploadFile } from 'antd/es/upload'

const { Title, Text } = Typography
const { Dragger } = Upload

interface EvalTaskSummary {
  id: string
  name: string
  description: string
  status: string
  metrics: string[]
  total_groups: number
  total_samples: number
  evaluated_groups: number
  created_at: string
}

async function fetchTasks(): Promise<{ items: EvalTaskSummary[] }> {
  const res = await fetch('/api/offline-eval/tasks')
  if (!res.ok) throw new Error('获取任务列表失败')
  return res.json()
}

async function deleteTask(taskId: string): Promise<void> {
  const res = await fetch(`/api/offline-eval/tasks/${taskId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('删除失败')
}

async function createTask(formData: FormData): Promise<{ task_id: string }> {
  const res = await fetch('/api/offline-eval/tasks', { method: 'POST', body: formData })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '创建任务失败')
  }
  return res.json()
}

const METRIC_OPTIONS = [
  { label: '总体 MOS（主观）', value: 'mos' },
  { label: '自然度 NMOS（主观）', value: 'nmos' },
  { label: '相似度 SMOS（主观，需参考音频）', value: 'smos' },
  { label: 'WER 词错误率（客观）', value: 'wer' },
  { label: 'SIM 说话人相似度（客观）', value: 'sim' },
]

const WER_MODELS = [
  { label: 'whisper-large-v3（英文/多语言）', value: 'whisper-large-v3' },
  { label: 'paraformer-zh（中文）', value: 'paraformer-zh' },
]

// ── Task List Tab ────────────────────────────────────────────────────────────

function TaskListTab() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['offline-eval-tasks'], queryFn: fetchTasks })

  const deleteMut = useMutation({
    mutationFn: deleteTask,
    onSuccess: () => {
      message.success('删除成功')
      qc.invalidateQueries({ queryKey: ['offline-eval-tasks'] })
    },
    onError: (e: Error) => message.error(e.message),
  })

  const columns = [
    {
      title: '任务名称',
      dataIndex: 'name',
      render: (name: string, row: EvalTaskSummary) => (
        <div>
          <div style={{ fontWeight: 600 }}>{name}</div>
          {row.description && <div style={{ fontSize: 12, color: '#8c8c8c' }}>{row.description}</div>}
        </div>
      ),
    },
    {
      title: '指标',
      dataIndex: 'metrics',
      render: (metrics: string[]) => (
        <Space size={4}>
          {metrics.map((m) => (
            <Tag key={m} color={m === 'mos' || m === 'smos' ? 'blue' : 'orange'}>
              {m.toUpperCase()}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '规模',
      render: (_: unknown, row: EvalTaskSummary) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {row.total_groups} 组 / {row.total_samples} 条
        </Text>
      ),
    },
    {
      title: '评价进度',
      render: (_: unknown, row: EvalTaskSummary) => (
        <div style={{ minWidth: 120 }}>
          <Progress
            percent={row.total_groups > 0 ? Math.round((row.evaluated_groups / row.total_groups) * 100) : 0}
            size="small"
            format={() => `${row.evaluated_groups}/${row.total_groups}`}
          />
        </div>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      render: (t: string) => new Date(t).toLocaleString('zh-CN', { hour12: false }),
    },
    {
      title: '操作',
      render: (_: unknown, row: EvalTaskSummary) => (
        <Space>
          <Button
            size="small"
            type="primary"
            icon={<PlayCircleOutlined />}
            onClick={() => navigate(`/offline-eval/${row.id}/evaluate`)}
          >
            开始评价
          </Button>
          <Button
            size="small"
            icon={<BarChartOutlined />}
            onClick={() => navigate(`/offline-eval/${row.id}/results`)}
          >
            查看结果
          </Button>
          <Popconfirm
            title="确认删除该评测任务及所有评分数据？"
            okText="删除"
            okType="danger"
            cancelText="取消"
            onConfirm={() => deleteMut.mutate(row.id)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Table
      rowKey="id"
      dataSource={data?.items ?? []}
      columns={columns}
      loading={isLoading}
      pagination={{ pageSize: 20 }}
      locale={{ emptyText: '暂无评测任务，请在"发布新任务"Tab中创建' }}
    />
  )
}

// ── Create Task Tab ──────────────────────────────────────────────────────────

function CreateTaskTab({ onCreated }: { onCreated: () => void }) {
  const [form] = Form.useForm()
  const [selectedMetrics, setSelectedMetrics] = useState<string[]>(['mos'])
  const [jsonlFile, setJsonlFile] = useState<UploadFile | null>(null)
  const [audioZip, setAudioZip] = useState<UploadFile | null>(null)
  const [loading, setLoading] = useState(false)

  const qc = useQueryClient()

  const handleSubmit = async () => {
    const values = await form.validateFields()
    if (!jsonlFile?.originFileObj) { message.error('请上传 JSONL 文件'); return }
    if (!audioZip?.originFileObj) { message.error('请上传音频 ZIP 文件'); return }

    const formData = new FormData()
    formData.append('name', values.name)
    formData.append('description', values.description || '')
    formData.append('metrics', JSON.stringify(selectedMetrics))
    if (selectedMetrics.includes('wer') && values.wer_model) {
      formData.append('wer_model', values.wer_model)
    }
    formData.append('jsonl_file', jsonlFile.originFileObj)
    formData.append('audio_zip', audioZip.originFileObj)

    setLoading(true)
    try {
      const res = await createTask(formData)
      message.success(`任务创建成功，共解析 ${res.task_id ? '' : ''}数据`)
      qc.invalidateQueries({ queryKey: ['offline-eval-tasks'] })
      form.resetFields()
      setJsonlFile(null)
      setAudioZip(null)
      setSelectedMetrics(['mos'])
      onCreated()
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const beforeUpload = () => false  // 阻止自动上传

  return (
    <Form form={form} layout="vertical" style={{ maxWidth: 640 }}>
      <Form.Item name="name" label="任务名称" rules={[{ required: true, message: '请输入任务名称' }]}>
        <Input placeholder="例：cosyvoice3 vs f5tts — L50测试集 2025-01" />
      </Form.Item>

      <Form.Item name="description" label="说明（可选）">
        <Input.TextArea rows={2} placeholder="任务背景、版本信息等" />
      </Form.Item>

      <Form.Item label="JSONL 文件" required>
        <Dragger
          accept=".jsonl"
          maxCount={1}
          beforeUpload={beforeUpload}
          fileList={jsonlFile ? [jsonlFile] : []}
          onChange={({ fileList }) => setJsonlFile(fileList[0] ?? null)}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">点击或拖拽上传 .jsonl 文件</p>
          <p className="ant-upload-hint">
            每行：&#123;"key":"t1_spk1","text":"...","speaker":"spk1","test_set":"L50","ref_audio":"refs/spk1.wav","results":[&#123;"model":"cosyvoice2","audio_path":"audio/cosyvoice2.wav"&#125;,...]&#125;
          </p>
        </Dragger>
      </Form.Item>

      <Form.Item label="音频 ZIP" required>
        <Dragger
          accept=".zip"
          maxCount={1}
          beforeUpload={beforeUpload}
          fileList={audioZip ? [audioZip] : []}
          onChange={({ fileList }) => setAudioZip(fileList[0] ?? null)}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">点击或拖拽上传 .zip 文件</p>
          <p className="ant-upload-hint">ZIP 内音频文件名须与 JSONL 中的 key 对应（如 abc123.wav）</p>
        </Dragger>
      </Form.Item>

      <Form.Item label="评测指标">
        <Checkbox.Group
          value={selectedMetrics}
          onChange={(v) => setSelectedMetrics(v as string[])}
          options={METRIC_OPTIONS}
        />
      </Form.Item>

      {selectedMetrics.includes('wer') && (
        <Form.Item name="wer_model" label="WER 识别模型" initialValue="whisper-large-v3">
          <Radio.Group options={WER_MODELS} />
        </Form.Item>
      )}

      {selectedMetrics.includes('sim') && (
        <Form.Item label="SIM 相似度模型">
          <Input disabled placeholder="待接入（功能开发中）" />
        </Form.Item>
      )}

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="SMOS 相似度评分需要 JSONL 每条记录包含 ref_audio 字段，指向对应音色的参考音频路径（ZIP 内相对路径）。"
      />

      <Form.Item>
        <Button type="primary" icon={<PlusOutlined />} onClick={handleSubmit} loading={loading}>
          发布评测任务
        </Button>
      </Form.Item>
    </Form>
  )
}

// ── Hub Page ──────────────────────────────────────────────────────────────────

export default function OfflineEvalHub() {
  const [activeTab, setActiveTab] = useState('list')

  return (
    <div>
      <Title level={4} style={{ marginBottom: 16 }}>离线评测</Title>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'list',
            label: '已发布任务',
            children: <TaskListTab />,
          },
          {
            key: 'create',
            label: '发布新任务',
            children: <CreateTaskTab onCreated={() => setActiveTab('list')} />,
          },
        ]}
      />
    </div>
  )
}
