import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Modal, Table, Tag, Typography } from 'antd'
import { api, TaskOut } from '../api/client'
import TaskStatusTag from '../components/TaskStatusTag'
import AudioPlayer from '../components/AudioPlayer'
import dayjs from 'dayjs'

const { Title, Text } = Typography

interface ResultItem { key: string; status: string; audio_url?: string; error?: string }

export default function TaskHistory() {
  const [page, setPage] = useState(1)
  const [detail, setDetail] = useState<TaskOut | null>(null)
  const pageSize = 20

  const { data, isLoading } = useQuery({
    queryKey: ['tasks-history', page],
    queryFn: () => api.listTasks({ limit: pageSize, offset: (page - 1) * pageSize }),
    refetchInterval: 10000,
  })

  const columns = [
    {
      title: '时间', dataIndex: 'created_at', key: 'created_at', width: 140,
      render: (v: string) => dayjs(v).format('MM-DD HH:mm:ss'),
    },
    {
      title: '任务', dataIndex: 'type', key: 'type', width: 120,
      render: (v: string, row: TaskOut) => {
        const label = (row.input as Record<string, unknown> | null)?.label as string | undefined
        if (label) return label
        if (v === 'tts') return '推理'
        if (v === 'prompt_experiment') return '克隆实验'
        return '数据处理'
      },
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 90,
      render: (v: string) => <TaskStatusTag status={v} />,
    },
    {
      title: '耗时', key: 'duration', width: 70,
      render: (_: unknown, row: TaskOut) => {
        if (!row.finished_at) return '—'
        const secs = dayjs(row.finished_at).diff(dayjs(row.created_at), 'second')
        return `${secs}s`
      },
    },
    {
      title: '操作', key: 'action',
      render: (_: unknown, row: TaskOut) => (
        <Button size="small" type="link" onClick={() => setDetail(row)}>详情</Button>
      ),
    },
  ]

  const result = detail?.result as { items: ResultItem[]; total: number; ok: number; fail: number } | null

  const resultColumns = [
    { title: 'Key', dataIndex: 'key', key: 'key', ellipsis: true },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 70,
      render: (v: string) => <TaskStatusTag status={v} />,
    },
    {
      title: '音频', key: 'audio', width: 70,
      render: (_: unknown, row: ResultItem) =>
        row.audio_url ? <AudioPlayer url={row.audio_url} /> : <span style={{ color: '#aaa' }}>—</span>,
    },
  ]

  return (
    <>
      <Title level={4} style={{ marginTop: 0 }}>历史任务</Title>
      <Card>
        <Table
          size="small"
          loading={isLoading}
          dataSource={data?.items ?? []}
          columns={columns}
          rowKey="id"
          pagination={{
            current: page,
            pageSize,
            total: data?.total ?? 0,
            onChange: setPage,
          }}
        />
      </Card>

      <Modal
        title={`任务详情 — ${detail?.id}`}
        open={!!detail}
        onCancel={() => setDetail(null)}
        footer={
          detail?.type === 'tts'
            ? <Link to={`/task/${detail.id}`} onClick={() => setDetail(null)}>
                <Button>完整结果页 ↗</Button>
              </Link>
            : null
        }
        width={800}
      >
        {detail && (
          <div>
            <p>
              <b>状态：</b><TaskStatusTag status={detail.status} />
              {result && (
                <span style={{ marginLeft: 12 }}>
                  <Tag color="green">成功 {result.ok}</Tag>
                  {result.fail > 0 && <Tag color="red">失败 {result.fail}</Tag>}
                  <Text type="secondary" style={{ fontSize: 12 }}> / 共 {result.total}</Text>
                </span>
              )}
            </p>
            <p>
              <b>时间：</b>{dayjs(detail.created_at).format('YYYY-MM-DD HH:mm:ss')}
              {detail.finished_at && ` → ${dayjs(detail.finished_at).format('HH:mm:ss')}`}
            </p>
            {detail.error && <p style={{ color: 'red' }}><b>错误：</b>{detail.error}</p>}

            {/* 音频结果 */}
            {result?.items && result.items.length > 0 && (
              <>
                <b>生成结果：</b>
                <Table
                  size="small"
                  dataSource={result.items}
                  columns={resultColumns}
                  rowKey="key"
                  pagination={{ pageSize: 10, size: 'small' }}
                  style={{ marginTop: 8, marginBottom: 12 }}
                />
              </>
            )}

            {/* 日志 */}
            <b>日志：</b>
            <div style={{ background: '#f5f5f5', padding: 8, maxHeight: 160, overflowY: 'auto', fontSize: 12, marginTop: 4, borderRadius: 4 }}>
              {detail.logs.length === 0
                ? <Text type="secondary">无日志</Text>
                : detail.logs.map((l, i) => (
                  <div key={i} style={{ color: l.level === 'error' ? 'red' : '#444' }}>
                    [{l.time.slice(11, 19)}] {l.msg}
                  </div>
                ))}
            </div>
          </div>
        )}
      </Modal>
    </>
  )
}
