/**
 * frontend/src/components/dataset/PreviewConfirmPanel.tsx
 *
 * Shown when SSE 'pause' event arrives for audio processing steps.
 * silence_trim: ThresholdCanvas per group — user sets threshold_db visually.
 * Other steps: WaveformCompare before/after waveforms.
 */
import React, { useState } from 'react'
import { Alert, Button, Descriptions, Space, Tag, Typography } from 'antd'
import { LeftOutlined, RightOutlined } from '@ant-design/icons'
import WaveformCompare from './WaveformPlayer'
import ThresholdCanvas from './ThresholdCanvas'

const { Title, Text } = Typography

export interface PreviewSample {
  key: string
  speaker_id: string
  before_url: string
  after_url: string
  duration_sec: number
  head_envelope?: number[]
  tail_envelope?: number[]
  head_transition_pos?: number
  tail_transition_pos?: number
  segment_url?: string
  tail_segment_url?: string
}

export interface ParamGroupPreview {
  group_id: string
  speaker_ids: string[]
  params: Record<string, unknown>
  samples: PreviewSample[]
}

export interface PreviewConfirmPanelProps {
  taskId: string
  step: string
  groups: ParamGroupPreview[]
  onAction: (action: 'approve' | 'reject', payload?: Record<string, unknown>) => void
}

const STEP_LABEL: Record<string, string> = {
  silence_trim:        '首尾静音替换',
  silence_trim_review: '首尾静音替换 — 效果确认',
  pause_compress:      '压缩停顿',
  pause_compress_review: '压缩停顿 — 效果确认',
  gain:                '音量增益',
  gain_review:         '音量增益 — 效果确认',
  segment:             '切句',
}

export default function PreviewConfirmPanel({
  step,
  groups,
  onAction,
}: PreviewConfirmPanelProps): React.ReactElement {
  const [groupIdx, setGroupIdx] = useState(0)

  // Per-group threshold state (silence_trim only)
  const [groupThresholds, setGroupThresholds] = useState<Record<string, number>>(() => {
    const init: Record<string, number> = {}
    for (const g of groups) {
      init[g.group_id] = typeof g.params?.threshold_db === 'number'
        ? (g.params.threshold_db as number)
        : -55
    }
    return init
  })

  if (groups.length === 0) {
    return <Alert type="info" message="无预览样本" />
  }

  const group = groups[groupIdx]
  const thresholdDb = groupThresholds[group.group_id] ?? -55

  const paramEntries = Object.entries(group.params).filter(
    ([k, v]) => v !== null && v !== undefined && k !== 'threshold_db'
  )

  const handleApprove = () => {
    if (step === 'silence_trim') {
      onAction('approve', { group_thresholds: groupThresholds })
    } else {
      onAction('approve')
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="large">
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Title level={5} style={{ margin: 0 }}>
          步骤：{STEP_LABEL[step] ?? step}
        </Title>
        <Space>
          <Button size="small" icon={<LeftOutlined />}
            disabled={groupIdx === 0} onClick={() => setGroupIdx(i => i - 1)} />
          <Text>参数组 {groupIdx + 1} / {groups.length}</Text>
          <Button size="small" icon={<RightOutlined />}
            disabled={groupIdx === groups.length - 1} onClick={() => setGroupIdx(i => i + 1)} />
        </Space>
      </div>

      {/* Group params */}
      <Descriptions size="small" bordered column={2}>
        <Descriptions.Item label="说话人数">{group.speaker_ids.length} 个</Descriptions.Item>
        {paramEntries.map(([k, v]) => (
          <Descriptions.Item key={k} label={k}>
            <Tag>{typeof v === 'number' ? (v as number).toFixed(2) : String(v)}</Tag>
          </Descriptions.Item>
        ))}
      </Descriptions>

      {/* Samples */}
      <div>
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
          {step === 'silence_trim'
            ? `样本（共 ${group.samples.length} 条）— 拖拽红线或拖动滑块调整静音阈值`
            : `样本对比（共 ${group.samples.length} 条）— 红色虚线为静音阈值`}
        </Text>

        {step === 'silence_trim' ? (
          group.samples.map(s => (
            <div key={s.key} style={{ marginBottom: 20 }}>
              <Text style={{ fontSize: 11, color: '#8c8c8c', display: 'block', marginBottom: 4 }}>
                {s.key}
              </Text>
              <ThresholdCanvas
                headEnvelope={s.head_envelope ?? []}
                headTransitionPos={s.head_transition_pos}
                tailEnvelope={s.tail_envelope ?? []}
                tailTransitionPos={s.tail_transition_pos}
                segmentUrl={s.segment_url}
                tailSegmentUrl={s.tail_segment_url}
                thresholdDb={thresholdDb}
                onThresholdChange={(db) =>
                  setGroupThresholds(prev => ({ ...prev, [group.group_id]: db }))
                }
                peakDb={typeof group.params.peak_db === 'number' ? group.params.peak_db as number : undefined}
                meanDb={typeof group.params.mean_db === 'number' ? group.params.mean_db as number : undefined}
                sampleRate={typeof group.params.sample_rate === 'number' ? group.params.sample_rate as number : undefined}
              />
            </div>
          ))
        ) : (
          // Before/after waveform view for other steps
          group.samples.map(s => (
            <div key={s.key} style={{ marginBottom: 16 }}>
              <Text style={{ fontSize: 11, color: '#8c8c8c' }}>{s.key}</Text>
              <WaveformCompare
                beforeUrl={s.before_url}
                afterUrl={s.after_url}
                thresholdDb={typeof group.params?.threshold_db === 'number'
                  ? (group.params.threshold_db as number) : -55}
              />
            </div>
          ))
        )}
      </div>

      {/* Actions */}
      <Space>
        <Button type="primary" onClick={handleApprove}>
          {step === 'silence_trim'
            ? '确认阈值，全量执行'
            : ['silence_trim_review', 'pause_compress_review', 'gain_review'].includes(step)
            ? '效果满意，继续下一步'
            : '参数正确，全量执行'}
        </Button>
      </Space>
    </Space>
  )
}
