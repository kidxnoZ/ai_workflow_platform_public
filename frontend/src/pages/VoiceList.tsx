import { useEffect, useState } from 'react'
import { Table, Button, Popconfirm, Tag, Typography, Space, message, Switch, Tabs } from 'antd'
import { DeleteOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import { api, ElevenLabsVoiceOut, MinimaxVoiceOut, DoubaoVoiceOut } from '../api/client'

const { Text } = Typography

export default function VoiceList() {
  const [elVoices, setElVoices] = useState<ElevenLabsVoiceOut[]>([])
  const [mmVoices, setMmVoices] = useState<MinimaxVoiceOut[]>([])
  const [dbVoices, setDbVoices] = useState<DoubaoVoiceOut[]>([])
  const [loading, setLoading] = useState(true)
  const [includeDeleted, setIncludeDeleted] = useState(false)

  const fetchVoices = async () => {
    setLoading(true)
    try {
      const data = await api.listVoices(includeDeleted)
      setElVoices(data.elevenlabs ?? [])
      setMmVoices(data.minimax ?? [])
      setDbVoices(data.doubao ?? [])
    } catch {
      message.error('加载音色列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchVoices() }, [includeDeleted])

  const handleDeleteEl = async (voiceId: string) => {
    try {
      await api.deleteVoice(voiceId)
      message.success('音色已删除')
      fetchVoices()
    } catch { message.error('删除失败') }
  }

  const handleDeleteMm = async (providerVoiceId: string) => {
    try {
      await api.deleteMinimaxVoice(providerVoiceId)
      message.success('音色已删除')
      fetchVoices()
    } catch { message.error('删除失败') }
  }

  const taskCell = (id: string | null) => id
    ? <Link to={`/tasks/${id}`} style={{ fontSize: 12 }}>{id.slice(0, 8)}…</Link>
    : <Text type="secondary">—</Text>

  const timeCell = (t: string) => new Date(t).toLocaleString('zh-CN')

  const statusCell = (t: string | null) => t
    ? <Tag color="default">已删除 {new Date(t).toLocaleDateString('zh-CN')}</Tag>
    : <Tag color="green">活跃</Tag>

  const elColumns = [
    { title: '音色名称', dataIndex: 'voice_name',
      render: (n: string) => <Text code style={{ fontSize: 12 }}>{n}</Text> },
    { title: 'Voice ID', dataIndex: 'voice_id',
      render: (id: string) => <Text copyable style={{ fontSize: 12 }}>{id}</Text> },
    { title: '内容 Hash', dataIndex: 'prompt_hash',
      render: (h: string) => <Text type="secondary" style={{ fontSize: 12 }}>{h}</Text> },
    { title: '关联任务', dataIndex: 'task_id', render: taskCell },
    { title: '创建时间', dataIndex: 'created_at', render: timeCell },
    { title: '状态', dataIndex: 'deleted_at', render: statusCell },
    {
      title: '操作', key: 'action',
      render: (_: unknown, r: ElevenLabsVoiceOut) => !r.deleted_at ? (
        <Popconfirm title="确认删除此音色？"
          description="将同步调用 ElevenLabs API 删除，且无法恢复。"
          onConfirm={() => handleDeleteEl(r.voice_id)}
          okText="删除" cancelText="取消" okButtonProps={{ danger: true }}>
          <Button type="text" danger size="small" icon={<DeleteOutlined />}>删除</Button>
        </Popconfirm>
      ) : null,
    },
  ]

  const mmColumns = [
    { title: 'Provider Voice ID', dataIndex: 'provider_voice_id',
      render: (id: string) => <Text copyable style={{ fontSize: 12 }}>{id}</Text> },
    { title: '内容 Hash', dataIndex: 'prompt_hash',
      render: (h: string) => <Text type="secondary" style={{ fontSize: 12 }}>{h}</Text> },
    { title: '关联任务', dataIndex: 'task_id', render: taskCell },
    { title: '创建时间', dataIndex: 'created_at', render: timeCell },
    { title: '状态', dataIndex: 'deleted_at', render: statusCell },
    {
      title: '操作', key: 'action',
      render: (_: unknown, r: MinimaxVoiceOut) => !r.deleted_at ? (
        <Popconfirm title="确认删除此音色？"
          description="将同步调用 Minimax API 删除，且无法恢复。"
          onConfirm={() => handleDeleteMm(r.provider_voice_id)}
          okText="删除" cancelText="取消" okButtonProps={{ danger: true }}>
          <Button type="text" danger size="small" icon={<DeleteOutlined />}>删除</Button>
        </Popconfirm>
      ) : null,
    },
  ]

  const dbColumns = [
    { title: 'Speaker ID', dataIndex: 'speaker_id',
      render: (id: string) => <Text copyable style={{ fontSize: 12 }}>{id}</Text> },
    { title: '内容 Hash', dataIndex: 'prompt_hash',
      render: (h: string) => <Text type="secondary" style={{ fontSize: 12 }}>{h}</Text> },
    { title: '关联任务', dataIndex: 'task_id', render: taskCell },
    { title: '创建时间', dataIndex: 'created_at', render: timeCell },
    { title: '状态', key: 'status', render: () => <Tag color="blue">记录中（暂无删除）</Tag> },
  ]

  const toolbar = (
    <Space>
      <span style={{ fontSize: 13, color: '#666' }}>显示已删除</span>
      <Switch checked={includeDeleted} onChange={setIncludeDeleted} size="small" />
      <Button onClick={fetchVoices} size="small">刷新</Button>
    </Space>
  )

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <Typography.Title level={4} style={{ margin: 0 }}>音色列表</Typography.Title>
        {toolbar}
      </div>
      <Tabs items={[
        {
          key: 'elevenlabs',
          label: `ElevenLabs (${elVoices.length})`,
          children: (
            <Table dataSource={elVoices} columns={elColumns} rowKey="voice_id"
              loading={loading} pagination={{ pageSize: 20 }} size="small"
              locale={{ emptyText: '暂无克隆音色' }} />
          ),
        },
        {
          key: 'minimax',
          label: `Minimax (${mmVoices.length})`,
          children: (
            <Table dataSource={mmVoices} columns={mmColumns} rowKey="provider_voice_id"
              loading={loading} pagination={{ pageSize: 20 }} size="small"
              locale={{ emptyText: '暂无克隆音色' }} />
          ),
        },
        {
          key: 'doubao',
          label: `Doubao (${dbVoices.length})`,
          children: (
            <Table dataSource={dbVoices} columns={dbColumns} rowKey="speaker_id"
              loading={loading} pagination={{ pageSize: 20 }} size="small"
              locale={{ emptyText: '暂无克隆音色' }} />
          ),
        },
      ]} />
    </div>
  )
}
