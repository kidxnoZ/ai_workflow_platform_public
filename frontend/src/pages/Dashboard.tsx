import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Card, Col, Popconfirm, Row, Statistic, Table, Tag, Typography, message } from 'antd'
import { api, BenchmarkRunListItem, TaskOut } from '../api/client'
import TaskStatusTag from '../components/TaskStatusTag'
import dayjs from 'dayjs'

const { Title, Text } = Typography

function benchmarkStatusColor(s: string) {
  const map: Record<string, string> = { pending: 'default', running: 'processing', partial: 'warning', done: 'success' }
  return map[s] ?? 'default'
}

export default function Dashboard() {
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data: tasksData } = useQuery({
    queryKey: ['tasks-all'],
    queryFn: () => api.listTasks({ limit: 100 }),
    refetchInterval: 5000,
  })

  const { data: benchmarksData } = useQuery({
    queryKey: ['benchmarks-list'],
    queryFn: () => api.listBenchmarks({ limit: 10 }),
    refetchInterval: 8000,
  })

  const tasks = tasksData?.items ?? []
  const byStatus = (s: string) => tasks.filter((t) => t.status === s).length
  const today = dayjs().format('YYYY-MM-DD')
  const todayTasks = tasks.filter((t) => t.created_at.startsWith(today))

  const taskColumns = [
    {
      title: '时间', dataIndex: 'created_at', key: 'created_at', width: 120,
      render: (v: string) => dayjs(v).format('MM-DD HH:mm'),
    },
    {
      title: '类型', dataIndex: 'type', key: 'type', width: 100,
      render: (v: string) => v === 'tts' ? '推理' : v === 'prompt_experiment_v3' ? '实验' : '数据处理',
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 90,
      render: (v: string) => <TaskStatusTag status={v} />,
    },
    {
      title: '操作', key: 'action', width: 70,
      render: (_: unknown, row: TaskOut) => {
        if (!['pending', 'running', 'awaiting_review'].includes(row.status)) return null
        return (
          <span onClick={(e) => e.stopPropagation()}>
            <Popconfirm title="终止此任务？" okText="终止" cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={async () => {
                try { await api.cancelTask(row.id); qc.invalidateQueries({ queryKey: ['tasks-all'] }); message.success('已终止') } catch { message.error('终止失败') }
              }}>
              <Button size="small" danger type="link">终止</Button>
            </Popconfirm>
          </span>
        )
      },
    },
  ]

  const benchmarkColumns = [
    {
      title: '时间', dataIndex: 'created_at', key: 'created_at', width: 120,
      render: (v: string) => dayjs(v).format('MM-DD HH:mm'),
    },
    {
      title: '名称/ID', key: 'name',
      render: (_: unknown, row: BenchmarkRunListItem) => (
        <Text style={{ fontSize: 12 }}>{row.name || row.id.slice(0, 8) + '…'}</Text>
      ),
    },
    {
      title: '模型', key: 'models',
      render: (_: unknown, row: BenchmarkRunListItem) => (
        <span>{row.model_ids.map((id) => <Tag key={id} style={{ fontSize: 11 }}>{id}</Tag>)}</span>
      ),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 90,
      render: (v: string) => <Tag color={benchmarkStatusColor(v)}>{v.toUpperCase()}</Tag>,
    },
    {
      title: '操作', key: 'action', width: 80,
      render: (_: unknown, row: BenchmarkRunListItem) => (
        <Button size="small" type="link" onClick={() => navigate(`/benchmark/${row.id}`)}>查看</Button>
      ),
    },
  ]

  return (
    <>
      <Title level={4} style={{ marginTop: 0 }}>Dashboard</Title>

      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={6}><Card><Statistic title="今日任务" value={todayTasks.length} /></Card></Col>
        <Col span={6}><Card><Statistic title="运行中" value={byStatus('running')} valueStyle={{ color: '#1677ff' }} /></Card></Col>
        <Col span={6}><Card><Statistic title="成功" value={byStatus('success')} valueStyle={{ color: '#52c41a' }} /></Card></Col>
        <Col span={6}><Card><Statistic title="失败" value={byStatus('failed')} valueStyle={{ color: '#ff4d4f' }} /></Card></Col>
      </Row>

      <Row gutter={16}>
        <Col span={12}>
          <Card
            title="最近任务"
            extra={<Button size="small" type="link" onClick={() => navigate('/history')}>全部 →</Button>}
          >
            <Table
              size="small"
              dataSource={tasks.slice(0, 8)}
              columns={taskColumns}
              rowKey="id"
              pagination={false}
              onRow={(row: TaskOut) => ({ onClick: () => navigate(`/task/${row.id}`) })}
              rowClassName={() => 'clickable-row'}
            />
          </Card>
        </Col>
        <Col span={12}>
          <Card
            title="Benchmark 历史"
            extra={<Button size="small" type="primary" onClick={() => navigate('/tts')}>新建 +</Button>}
          >
            <Table
              size="small"
              dataSource={benchmarksData?.items ?? []}
              columns={benchmarkColumns}
              rowKey="id"
              pagination={false}
              locale={{ emptyText: '暂无 Benchmark 记录' }}
            />
          </Card>
        </Col>
      </Row>
    </>
  )
}
