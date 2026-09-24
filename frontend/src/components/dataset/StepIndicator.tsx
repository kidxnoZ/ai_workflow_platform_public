/**
 * frontend/src/components/dataset/StepIndicator.tsx
 */
import React from 'react'
import { Steps, Tooltip } from 'antd'
import { HistoryOutlined } from '@ant-design/icons'

export const STEP_LABELS: Record<string, string> = {
  classify:      '数据分类',
  metadata:      'Metadata 标注',
  silence_trim:  '首尾静音',
  segment:       '切句',
  asr:           'ASR 转录',
  pause_compress:'压缩停顿',
  gain:          '音量增益',
  flag_short:    '过短标注',
  flag_richtext: '富文本标注',
  review:        '人工校对',
}

export interface StepIndicatorProps {
  steps: string[]
  currentStep: string | null
  completedSteps: string[]
  historySteps?: string[]
  selectedHistoryStep?: string | null
  onStepClick?: (step: string) => void
}

export default function StepIndicator({
  steps, currentStep, completedSteps,
  historySteps = [], selectedHistoryStep = null, onStepClick,
}: StepIndicatorProps): React.ReactElement {
  const currentIndex = currentStep ? steps.indexOf(currentStep) : -1

  const items = steps.map((key, idx) => {
    const isCompleted = completedSteps.includes(key)
    const isCurrent = key === currentStep
    const hasHistory = historySteps.includes(key)
    const isSelected = key === selectedHistoryStep

    const title = (
      <Tooltip title={hasHistory ? '点击查看历史结果' : undefined} placement="bottom">
        <span
          onClick={() => hasHistory && onStepClick?.(key)}
          style={{
            cursor: hasHistory ? 'pointer' : 'default',
            fontWeight: isSelected ? 700 : undefined,
            color: isSelected ? '#1677ff' : undefined,
            display: 'inline-flex', alignItems: 'center', gap: 3,
          }}
        >
          {STEP_LABELS[key] ?? key}
          {hasHistory && !isSelected && (
            <HistoryOutlined style={{ fontSize: 10, opacity: 0.45 }} />
          )}
        </span>
      </Tooltip>
    )

    return {
      title,
      status: isCompleted ? ('finish' as const)
        : isCurrent ? ('process' as const)
        : idx < currentIndex ? ('finish' as const)
        : ('wait' as const),
    }
  })

  return (
    <Steps
      current={currentIndex}
      items={items}
      size="small"
      style={{ marginBottom: 16 }}
    />
  )
}
