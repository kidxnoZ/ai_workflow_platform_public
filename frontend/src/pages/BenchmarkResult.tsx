import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Progress, Space, Table, Tag, Typography } from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { api, BenchmarkRunOut, BenchmarkTaskSummary } from '../api/client'
import TaskStatusTag from '../components/TaskStatusTag'
import AudioPlayer from '../components/AudioPlayer'

const { Title, Text } = Typography

type BenchmarkStatus = 'pending' | 'running' | 'partial' | 'done'

function statusColor(s: BenchmarkStatus | string) {
  const map: Record<string, string> = {
    pending: 'default', running: 'processing', partial: 'warning', done: 'success',
  }
  return map[s] ?? 'default'
}

function pctOf(t: BenchmarkTaskSummary) {
  if (!t.total) return 0
  return Math.round(((t.ok ?? 0) / t.total) * 100)
}

export default function BenchmarkResult() {
  const { runId } = useParams<{ runId: string }>()

  const { data: run } = useQuery<BenchmarkRunOut>({
    queryKey: ['benchmark', runId],
    queryFn: () => api.getBenchmark(runId!),
    enabled: !!runId,
    refetchInterval: (q) => {
      const s = (q.state.data as BenchmarkRunOut | undefined)?.status
      return s === 'running' || s === 'pending' ? 3000 : false
    },
  })

  if (!run) {
    return <Text type="secondary">加载中…</Text>
  }

  const allDone = run.status === 'done' || run.status === 'partial'
  const doneCount = run.tasks.filter((t) => t.status === 'success' || t.status === 'failed').length

  const columns = [
    {
      title: '模型', dataIndex: 'model_id', key: 'model_id', width: 160,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 100,
      render: (v: string) => <TaskStatusTag status={v} />,
    },
    {
      title: '进度', key: 'progress', width: 160,
      render: (_: unknown, row: BenchmarkTaskSummary) => {
        if (!row.total) return <Text type="secondary">—</Text>
        const pct = pctOf(row)
        return (
          <Space size={4}>
            <Progress
              percent={pct}
              size="small"
              style={{ width: 80 }}
              status={row.status === 'failed' ? 'exception' : row.status === 'success' ? 'success' : 'active'}
            />
            <Text style={{ fontSize: 12 }}>{row.ok ?? 0}/{row.total}</Text>
          </Space>
        )
      },
    },
    {
      title: '成功率', key: 'rate', width: 80,
      render: (_: unknown, row: BenchmarkTaskSummary) => {
        if (!row.total) return '—'
        return <Text>{pctOf(row)}%</Text>
      },
    },
    {
      title: '操作', key: 'action', width: 120,
      render: (_: unknown, row: BenchmarkTaskSummary) => (
        <Link to={`/task/${row.task_id}`}>
          <Button size="small">查看详情</Button>
        </Link>
      ),
    },
  ]

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        <Link to="/tts">
          <Button icon={<ArrowLeftOutlined />} size="small">返回</Button>
        </Link>
        <Title level={4} style={{ margin: 0 }}>Benchmark 结果</Title>
        <Tag color={statusColor(run.status)}>{run.status.toUpperCase()}</Tag>
      </Space>

      <Card style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Text type="secondary">Run ID: {run.id}</Text>
          {run.name && <Text>名称: {run.name}</Text>}
          <Text>模型数: {run.tasks.length} · 已完成: {doneCount}/{run.tasks.length}</Text>
          {!allDone && (
            <Progress
              percent={Math.round((doneCount / (run.tasks.length || 1)) * 100)}
              status="active"
              style={{ maxWidth: 400 }}
            />
          )}
        </Space>
      </Card>

      <Table
        dataSource={run.tasks}
        columns={columns}
        rowKey="task_id"
        size="small"
        pagination={false}
      />
    </>
  )
}
