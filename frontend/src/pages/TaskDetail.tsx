import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Progress, Space, Table, Typography } from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { api, TaskOut } from '../api/client'
import TaskStatusTag from '../components/TaskStatusTag'
import AudioPlayer from '../components/AudioPlayer'

const { Title, Text } = Typography

interface ResultItem { key: string; status: string; audio_url?: string; error?: string }

export default function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>()

  const { data: task } = useQuery<TaskOut>({
    queryKey: ['task', taskId],
    queryFn: () => api.getTask(taskId!),
    enabled: !!taskId,
    refetchInterval: (q) => {
      const s = (q.state.data as TaskOut | undefined)?.status
      return s === 'running' || s === 'pending' ? 2000 : false
    },
  })

  if (!task) return <Text type="secondary">加载中…</Text>

  const result = task.result as { items: ResultItem[]; total: number; ok: number; fail: number } | null
  const logs = task.logs ?? []
  const lastLog = logs[logs.length - 1]?.msg ?? ''
  const progressPct = result
    ? Math.round(((result.ok ?? 0) + (result.fail ?? 0)) / (result.total || 1) * 100)
    : undefined

  const columns = [
    { title: 'Key', dataIndex: 'key', key: 'key', ellipsis: true, width: 220 },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 80,
      render: (v: string) => <TaskStatusTag status={v} />,
    },
    {
      title: '音频', key: 'audio', width: 100,
      render: (_: unknown, row: ResultItem) =>
        row.audio_url ? <AudioPlayer url={row.audio_url} /> : <span style={{ color: '#aaa' }}>—</span>,
    },
    {
      title: '错误', dataIndex: 'error', key: 'error', ellipsis: true,
      render: (v: string) => v ? <Text type="danger" style={{ fontSize: 12 }}>{v}</Text> : null,
    },
  ]

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ArrowLeftOutlined />} size="small" onClick={() => history.back()}>返回</Button>
        <Title level={4} style={{ margin: 0 }}>任务详情</Title>
        <TaskStatusTag status={task.status} />
      </Space>

      <Card style={{ marginBottom: 16 }}>
        <Space direction="vertical">
          <Text type="secondary">ID: {task.id}</Text>
          <Text type="secondary">创建: {new Date(task.created_at).toLocaleString()}</Text>
          {task.finished_at && <Text type="secondary">完成: {new Date(task.finished_at).toLocaleString()}</Text>}
        </Space>
      </Card>

      {(task.status === 'running' || task.status === 'pending') && (
        <Progress percent={progressPct} status="active" style={{ marginBottom: 8 }} />
      )}
      {lastLog && <div style={{ color: '#888', marginBottom: 12, fontSize: 12 }}>{lastLog}</div>}
      {task.error && <Alert type="error" message={task.error} style={{ marginBottom: 12 }} />}

      {result && (
        <>
          <Text style={{ marginBottom: 8, display: 'block' }}>
            共 {result.total} 条 · 成功 {result.ok} · 失败 {result.fail}
          </Text>
          <Table
            size="small"
            dataSource={result.items}
            columns={columns}
            rowKey="key"
            pagination={{ pageSize: 50 }}
          />
        </>
      )}
    </>
  )
}
