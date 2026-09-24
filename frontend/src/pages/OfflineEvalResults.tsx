import { useParams, useNavigate } from 'react-router-dom'
import { Table, Tabs, Tag, Spin, Alert, Button, Typography, Space, Statistic, Row, Col, Select, Segmented } from 'antd'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

const { Title, Text } = Typography

interface ModelStats {
  count: number
  avg_mos: number | null
  avg_nmos: number | null
  avg_smos: number | null
  avg_wer: number | null
  avg_sim: number | null
}

interface ResultsData {
  task_id: string
  task_name: string
  metrics: string[]
  total_ratings: number
  evaluated_groups: number
  total_groups: number
  by_model: Record<string, ModelStats>
  by_test_set: Record<string, Record<string, ModelStats>>
  by_gender: Record<string, Record<string, ModelStats>>
  by_age: Record<string, Record<string, ModelStats>>
  by_gender_age: Record<string, Record<string, ModelStats>>
}

async function fetchResults(taskId: string): Promise<ResultsData> {
  const res = await fetch(`/api/offline-eval/tasks/${taskId}/results`)
  if (!res.ok) throw new Error('获取结果失败')
  return res.json()
}

function fmtScore(v: number | null, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return v.toFixed(digits)
}

function scoreColor(v: number | null, metric: string): string {
  if (v === null) return ''
  if (metric === 'wer') {
    if (v <= 0.05) return '#52c41a'
    if (v <= 0.15) return '#faad14'
    return '#ff4d4f'
  }
  // MOS / SMOS / SIM: higher is better (SIM ~0-1, MOS 1-5)
  const norm = metric === 'sim' ? v : (v - 1) / 4
  if (norm >= 0.7) return '#52c41a'
  if (norm >= 0.4) return '#faad14'
  return '#ff4d4f'
}

// ── Overall by Model ──────────────────────────────────────────────────────────

function ByModelTab({ data }: { data: ResultsData }) {
  const models = Object.keys(data.by_model)
  const metrics = data.metrics

  const columns = [
    {
      title: '模型',
      dataIndex: 'model',
      render: (m: string) => <Tag color="blue">{m}</Tag>,
      sorter: (a: { model: string }, b: { model: string }) => a.model.localeCompare(b.model),
    },
    { title: '评分次数', dataIndex: 'count', sorter: (a: ModelStats & { model: string }, b: ModelStats & { model: string }) => a.count - b.count },
    ...(metrics.includes('mos') ? [{
      title: 'avg MOS',
      dataIndex: 'avg_mos',
      render: (v: number | null) => <span style={{ color: scoreColor(v, 'mos'), fontWeight: 600 }}>{fmtScore(v)}</span>,
      sorter: (a: ModelStats, b: ModelStats) => (a.avg_mos ?? -1) - (b.avg_mos ?? -1),
    }] : []),
    ...(metrics.includes('nmos') ? [{
      title: 'avg NMOS',
      dataIndex: 'avg_nmos',
      render: (v: number | null) => <span style={{ color: scoreColor(v, 'nmos'), fontWeight: 600 }}>{fmtScore(v)}</span>,
      sorter: (a: ModelStats, b: ModelStats) => (a.avg_nmos ?? -1) - (b.avg_nmos ?? -1),
    }] : []),
    ...(metrics.includes('smos') ? [{
      title: 'avg SMOS',
      dataIndex: 'avg_smos',
      render: (v: number | null) => <span style={{ color: scoreColor(v, 'smos'), fontWeight: 600 }}>{fmtScore(v)}</span>,
      sorter: (a: ModelStats, b: ModelStats) => (a.avg_smos ?? -1) - (b.avg_smos ?? -1),
    }] : []),
    ...(metrics.includes('wer') ? [{
      title: 'avg WER',
      dataIndex: 'avg_wer',
      render: (v: number | null) => <span style={{ color: scoreColor(v, 'wer'), fontWeight: 600 }}>{fmtScore(v)}</span>,
      sorter: (a: ModelStats, b: ModelStats) => (a.avg_wer ?? 999) - (b.avg_wer ?? 999),
    }] : []),
    ...(metrics.includes('sim') ? [{
      title: 'avg SIM',
      dataIndex: 'avg_sim',
      render: (v: number | null) => <span style={{ color: scoreColor(v, 'sim'), fontWeight: 600 }}>{fmtScore(v)}</span>,
      sorter: (a: ModelStats, b: ModelStats) => (a.avg_sim ?? -1) - (b.avg_sim ?? -1),
    }] : []),
  ]

  const rows = models.map((m) => ({ model: m, ...data.by_model[m] }))

  return <Table rowKey="model" dataSource={rows} columns={columns} pagination={false} size="middle" />
}

// ── Pivot Table (test_set or speaker_type) ────────────────────────────────────

function PivotTab({
  pivot,
  metrics,
  pivotLabel,
}: {
  pivot: Record<string, Record<string, ModelStats>>
  metrics: string[]
  pivotLabel: string
}) {
  const [selectedMetric, setSelectedMetric] = useState<string>(metrics[0] ?? 'mos')
  const metricKey = `avg_${selectedMetric}` as keyof ModelStats

  const allModels = Array.from(
    new Set(Object.values(pivot).flatMap((m) => Object.keys(m)))
  ).sort()
  const pivotKeys = Object.keys(pivot).sort()

  const columns = [
    { title: pivotLabel, dataIndex: '__pivot__', render: (v: string) => <Tag>{v}</Tag> },
    ...allModels.map((m) => ({
      title: <Tag color="blue">{m}</Tag>,
      dataIndex: m,
      render: (v: number | null) => (
        <span style={{ color: scoreColor(v, selectedMetric), fontWeight: v !== null ? 600 : 400 }}>
          {fmtScore(v)}
        </span>
      ),
    })),
  ]

  const rows = pivotKeys.map((pk) => {
    const row: Record<string, unknown> = { __pivot__: pk }
    for (const m of allModels) {
      const stats = pivot[pk]?.[m]
      row[m] = stats ? (stats[metricKey] as number | null) : null
    }
    return row
  })

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Text strong style={{ marginRight: 8 }}>显示指标：</Text>
        <Select
          value={selectedMetric}
          onChange={setSelectedMetric}
          size="small"
          style={{ width: 120 }}
          options={metrics.map((m) => ({ value: m, label: m.toUpperCase() }))}
        />
      </div>
      <Table
        rowKey="__pivot__"
        dataSource={rows}
        columns={columns}
        pagination={false}
        size="middle"
        scroll={{ x: true }}
      />
    </div>
  )
}

// ── Demographics Cross Table ──────────────────────────────────────────────────

type DimKey = 'gender' | 'age' | 'gender_age'

function DemographicsTab({ data }: { data: ResultsData }) {
  const [dim, setDim] = useState<DimKey>('gender')
  const [selectedMetric, setSelectedMetric] = useState<string>(data.metrics[0] ?? 'mos')
  const metricKey = `avg_${selectedMetric}` as keyof ModelStats

  const pivotMap: Record<DimKey, Record<string, Record<string, ModelStats>>> = {
    gender: data.by_gender,
    age: data.by_age,
    gender_age: data.by_gender_age,
  }
  const dimLabel: Record<DimKey, string> = {
    gender: '性别',
    age: '年龄',
    gender_age: '性别 × 年龄',
  }

  const pivot = pivotMap[dim]
  const allModels = Array.from(
    new Set(Object.values(pivot).flatMap((m) => Object.keys(m)))
  ).sort()
  const dimKeys = Object.keys(pivot).sort()

  // "全部" 行取 by_model 汇总
  const totalRow: Record<string, unknown> = { __pivot__: '全部 (汇总)' }
  for (const m of allModels) {
    totalRow[m] = data.by_model[m] ? (data.by_model[m][metricKey] as number | null) : null
  }

  const dataRows = dimKeys.map((pk) => {
    const row: Record<string, unknown> = { __pivot__: pk }
    for (const m of allModels) {
      const stats = pivot[pk]?.[m]
      row[m] = stats ? (stats[metricKey] as number | null) : null
    }
    return row
  })

  const columns = [
    {
      title: dimLabel[dim],
      dataIndex: '__pivot__',
      width: 160,
      render: (v: string) => (
        v === '全部 (汇总)'
          ? <Tag color="purple" style={{ fontWeight: 600 }}>{v}</Tag>
          : <Tag>{v}</Tag>
      ),
    },
    ...allModels.map((m) => ({
      title: <Tag color="blue">{m}</Tag>,
      dataIndex: m,
      align: 'center' as const,
      render: (v: number | null) => (
        <span style={{ color: scoreColor(v, selectedMetric), fontWeight: v !== null ? 600 : 400 }}>
          {fmtScore(v)}
        </span>
      ),
    })),
  ]

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 14, flexWrap: 'wrap' }}>
        <div>
          <Text strong style={{ marginRight: 8 }}>分析维度：</Text>
          <Segmented
            value={dim}
            onChange={(v) => setDim(v as DimKey)}
            options={[
              { value: 'gender', label: '按性别' },
              { value: 'age', label: '按年龄' },
              { value: 'gender_age', label: '性别 × 年龄' },
            ]}
          />
        </div>
        <div>
          <Text strong style={{ marginRight: 8 }}>显示指标：</Text>
          <Select
            value={selectedMetric}
            onChange={setSelectedMetric}
            size="small"
            style={{ width: 120 }}
            options={data.metrics.map((m) => ({ value: m, label: m.toUpperCase() }))}
          />
        </div>
      </div>
      <Table
        rowKey="__pivot__"
        dataSource={[...dataRows, totalRow]}
        columns={columns}
        pagination={false}
        size="middle"
        scroll={{ x: true }}
        rowClassName={(row) => (row.__pivot__ === '全部 (汇总)' ? 'ant-table-row-selected' : '')}
      />
    </div>
  )
}

// ── Results Page ──────────────────────────────────────────────────────────────

export default function OfflineEvalResults() {
  const { taskId } = useParams<{ taskId: string }>()
  const navigate = useNavigate()

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['offline-eval-results', taskId],
    queryFn: () => fetchResults(taskId!),
    enabled: !!taskId,
    refetchInterval: 10000,
  })

  if (isLoading) {
    return <div style={{ textAlign: 'center', padding: 60 }}><Spin size="large" /></div>
  }

  if (error || !data) {
    return <Alert type="error" message="加载结果失败" action={<Button onClick={() => refetch()}>重试</Button>} />
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/offline-eval')} />
        <Title level={4} style={{ margin: 0 }}>{data.task_name} — 结果看板</Title>
        <Button icon={<ReloadOutlined />} size="small" onClick={() => refetch()} />
      </div>

      {/* Summary stats */}
      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col>
          <Statistic title="总评价条数" value={data.total_ratings} />
        </Col>
        <Col>
          <Statistic title="已评组数" value={`${data.evaluated_groups} / ${data.total_groups}`} />
        </Col>
        <Col>
          <Statistic title="参评模型数" value={Object.keys(data.by_model).length} />
        </Col>
        <Col>
          <div>
            <div style={{ fontSize: 12, color: '#8c8c8c' }}>评测指标</div>
            <Space style={{ marginTop: 4 }}>
              {data.metrics.map((m) => (
                <Tag key={m} color={m === 'mos' || m === 'smos' ? 'blue' : 'orange'}>{m.toUpperCase()}</Tag>
              ))}
            </Space>
          </div>
        </Col>
      </Row>

      {data.total_ratings === 0 ? (
        <Alert
          type="info"
          message="尚无评分数据"
          description="请先进行评价再查看结果。"
          action={
            <Button type="primary" onClick={() => navigate(`/offline-eval/${taskId}/evaluate`)}>
              开始评价
            </Button>
          }
        />
      ) : (
        <Tabs
          items={[
            {
              key: 'model',
              label: '总体模型对比',
              children: <ByModelTab data={data} />,
            },
            {
              key: 'test_set',
              label: '按测试集',
              children: (
                <PivotTab pivot={data.by_test_set} metrics={data.metrics} pivotLabel="测试集" />
              ),
            },
            {
              key: 'demographics',
              label: '性别 & 年龄',
              children: <DemographicsTab data={data} />,
            },
          ]}
        />
      )}
    </div>
  )
}
