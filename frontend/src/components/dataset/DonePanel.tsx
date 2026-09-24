/**
 * frontend/src/components/dataset/DonePanel.tsx
 *
 * Final summary panel: completion metrics table, duration distribution report,
 * and download buttons (data.jsonl, special cases list, hardcode errors list).
 */
import React from 'react'
import { Result, Descriptions, Table, Button, Typography, Space } from 'antd'
import { DownloadOutlined } from '@ant-design/icons'

const { Text } = Typography

export interface DoneSummary {
  initial_count: number
  role_count: number
  role_audio_count: number
  special_count: number
  integrity_ok: boolean
  metadata_ok_count: number
  sample_rate_zero_count: number
  duration_zero_count: number
  jsonl_total_rows: number
  hardcode_error_count: number
  output_path: string
  duration_distribution: Record<string, number> // bucket label → role count
  top_roles: Array<{ name: string; count: number; duration_h: number }>
  total_duration_h: number
}

export interface DonePanelProps {
  taskId: string
  summary: DoneSummary
}

export default function DonePanel({ taskId, summary }: DonePanelProps): React.ReactElement {
  const distColumns = [
    { title: '时长区间', dataIndex: 'bucket', key: 'bucket' },
    {
      title: '角色数',
      dataIndex: 'count',
      key: 'count',
      render: (v: number) => `${v} 个角色`,
    },
  ]

  const distData = Object.entries(summary.duration_distribution).map(
    ([bucket, count]) => ({ key: bucket, bucket, count }),
  )

  const topColumns = [
    { title: '角色名', dataIndex: 'name', key: 'name' },
    { title: '文件数', dataIndex: 'count', key: 'count' },
    {
      title: '总时长',
      dataIndex: 'duration_h',
      key: 'duration_h',
      render: (v: number) => `${v.toFixed(2)} h`,
    },
  ]

  const topData = summary.top_roles.map((r, i) => ({
    key: i,
    ...r,
  }))

  const downloadBase = `/api/dataset-ingest/${taskId}/download`

  return (
    <div style={{ maxWidth: 800 }}>
      <Result status="success" title="处理完成" />

      <Descriptions bordered column={2} size="small" style={{ marginBottom: 24 }}>
        <Descriptions.Item label="初始音频文件数">
          {summary.initial_count.toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="最终角色目录数">
          {summary.role_count.toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="role/audio 文件数">
          {summary.role_audio_count.toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="_special/ 文件数">
          {summary.special_count.toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="完整性校验">
          {summary.integrity_ok ? '✓ 通过' : '✗ 未通过'}
        </Descriptions.Item>
        <Descriptions.Item label="有 metadata 的角色">
          {summary.metadata_ok_count.toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="sample_rate=0 告警">
          {summary.sample_rate_zero_count.toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="duration=0 告警">
          {summary.duration_zero_count.toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="data.jsonl 总行数">
          {summary.jsonl_total_rows.toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="硬编码有误条目">
          {summary.hardcode_error_count.toLocaleString()}
        </Descriptions.Item>
      </Descriptions>

      <Text strong style={{ display: 'block', marginBottom: 8 }}>时长分布</Text>
      <Table
        columns={distColumns}
        dataSource={distData}
        pagination={false}
        size="small"
        style={{ marginBottom: 24 }}
      />

      <Text strong style={{ display: 'block', marginBottom: 8 }}>Top 10 最长角色</Text>
      <Table
        columns={topColumns}
        dataSource={topData.slice(0, 10)}
        pagination={false}
        size="small"
        style={{ marginBottom: 24 }}
      />

      <Text type="secondary" style={{ display: 'block', marginBottom: 16 }}>
        输出路径：{summary.output_path}
      </Text>

      <Space wrap>
        <a href={`${downloadBase}/jsonl`} download>
          <Button icon={<DownloadOutlined />}>下载合并 data.jsonl</Button>
        </a>
        <a href={`${downloadBase}/special`} download>
          <Button icon={<DownloadOutlined />}>下载特殊数据列表</Button>
        </a>
        <a href={`${downloadBase}/errors`} download>
          <Button icon={<DownloadOutlined />}>下载硬编码有误列表</Button>
        </a>
      </Space>
    </div>
  )
}
