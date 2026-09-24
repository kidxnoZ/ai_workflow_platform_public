/**
 * frontend/src/pages/DataProcess.tsx
 *
 * /dataset page: orchestrates the full ingest workflow.
 * State machine: form → executing (SSE) → review → done.
 *
 * Layout: left/right split (Clone Lab style)
 *   Left (~40%, always visible during executing/review/done):
 *     AgentLogStream — agent thinking timeline with elapsed timers
 *   Right (~60%):
 *     [form]      NewIngestForm
 *     [executing] StepIndicator + ActionPanel (or "等待 Agent 处理中…" placeholder)
 *     [review]    ReviewPlayer
 *     [done]      DonePanel
 */
import React, { useState, useCallback, useEffect } from 'react'
import { Alert, Button, Input, message, Space, Spin, Table, Tag, Typography } from 'antd'
import NewIngestForm from '../components/dataset/NewIngestForm'
import AgentLogStream from '../components/dataset/AgentLogStream'
import StepIndicator from '../components/dataset/StepIndicator'
import ClassifyConfirmPanel, {
  type MergeCandidate,
} from '../components/dataset/ClassifyConfirmPanel'
import PreviewConfirmPanel from '../components/dataset/PreviewConfirmPanel'
import DatasetBrowser from '../components/dataset/DatasetBrowser'
import ReviewPlayer from '../components/dataset/ReviewPlayer'
import DonePanel, { type DoneSummary } from '../components/dataset/DonePanel'
import { submitAction, cancelTask, getMetadataPreview, getAsrPreview } from '../api/datasetIngest'
import type { SSEEvent } from '../api/datasetIngest'

const { TextArea } = Input

// ── Constants ─────────────────────────────────────────────────────────────────

/** Pipeline steps in order — matches the new backend phase chain. */
const ALL_STEPS = [
  'classify',
  'metadata',
  'silence_trim',
  'segment',
  'asr',
  'pause_compress',
  'gain',
  'flag_short',
  'flag_richtext',
  'review',
]

type PageState = 'form' | 'executing' | 'review' | 'done'

// ── SegmentConfirmPanel (inline) ──────────────────────────────────────────────

function SegmentConfirmPanel({ taskId, defaultThreshold, onDismiss }: {
  taskId: string; defaultThreshold: number; onDismiss: () => void
}): React.ReactElement {
  const [thresholdSec, setThresholdSec] = useState(defaultThreshold)
  return (
    <div style={{ background: '#fff', borderRadius: 8, border: '1px solid #d9d9d9', padding: 16 }}>
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 8 }}>✂️ 切句确认</div>
      <div style={{ fontSize: 12, color: '#595959', marginBottom: 16 }}>
        检测音频内部停顿，超过阈值时长的位置自动切分为独立文件。切分后将对每个片段重新进行 ASR 转写。
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
        <span style={{ fontSize: 12, flexShrink: 0 }}>停顿切分阈值</span>
        <input type="range" min={0.5} max={10} step={0.5} value={thresholdSec}
          onChange={e => setThresholdSec(Number(e.target.value))} style={{ flex: 1 }} />
        <span style={{ fontSize: 13, fontWeight: 600, flexShrink: 0, minWidth: 50 }}>
          {thresholdSec.toFixed(1)} 秒
        </span>
      </div>
      <Space>
        <Button type="primary" onClick={async () => {
          try {
            await submitAction(taskId, { action: 'approve', step: 'segment_confirm',
              feedback: JSON.stringify({ threshold_sec: thresholdSec }) })
            onDismiss()
          } catch { message.error('操作失败') }
        }}>确认切句</Button>
        <Button onClick={async () => {
          try {
            await submitAction(taskId, { action: 'reject', step: 'segment_confirm' })
            onDismiss()
          } catch { message.error('操作失败') }
        }}>跳过切句</Button>
      </Space>
    </div>
  )
}

// ── GenericApprovePanel (inline) ──────────────────────────────────────────────

/** Simple approve / reject panel shown for steps that don't have a dedicated
 *  confirmation component (e.g. classify_review or unknown pause types). */
function GenericApprovePanel({
  taskId,
  step,
  onDismiss,
}: {
  taskId: string
  step: string
  onDismiss: () => void
}): React.ReactElement {
  const [rejecting, setRejecting] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const handleApprove = async () => {
    setSubmitting(true)
    try {
      await submitAction(taskId, { action: 'approve', step })
      onDismiss()
    } catch {
      message.error('操作失败')
    } finally {
      setSubmitting(false)
    }
  }

  const handleReject = async () => {
    if (!feedback.trim()) return
    setSubmitting(true)
    try {
      await submitAction(taskId, {
        action: 'reject',
        step,
        feedback: feedback.trim(),
      })
      onDismiss()
    } catch {
      message.error('操作失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      style={{
        padding: 16,
        background: '#fff',
        borderRadius: 8,
        border: '1px solid #d9d9d9',
      }}
    >
      {!rejecting ? (
        <Space>
          <Button type="primary" loading={submitting} onClick={handleApprove}>
            通过
          </Button>
          <Button
            danger
            onClick={() => setRejecting(true)}
            disabled={submitting}
          >
            有问题
          </Button>
        </Space>
      ) : (
        <Space direction="vertical" style={{ width: '100%' }}>
          <TextArea
            rows={3}
            placeholder="描述问题..."
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
          />
          <Space>
            <Button
              type="primary"
              loading={submitting}
              onClick={handleReject}
              disabled={!feedback.trim()}
            >
              提交修正
            </Button>
            <Button
              onClick={() => {
                setRejecting(false)
                setFeedback('')
              }}
              disabled={submitting}
            >
              取消
            </Button>
          </Space>
        </Space>
      )}
    </div>
  )
}

// ── SandboxErrorPanel (inline) ────────────────────────────────────────────────

/** Shown when sandbox rejects the script after all auto-fix retries.
 *  Lets the user edit the script manually and resubmit, or force-execute
/** Shown when sandbox rejects the script after all auto-fix retries.
 *  Lets the user edit the script manually and resubmit. */
function SandboxErrorPanel({
  taskId,
  error,
  script,
  onDismiss,
}: {
  taskId: string
  error: string
  script: string
  onDismiss: () => void
}): React.ReactElement {
  const [editedScript, setEditedScript] = useState(script)
  const [submitting, setSubmitting] = useState(false)

  const handleResubmit = async () => {
    setSubmitting(true)
    try {
      await submitAction(taskId, {
        action: 'resubmit_script',
        step: 'sandbox_error',
        feedback: editedScript,
      })
      onDismiss()
    } catch {
      message.error('提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div style={{ background: '#fff', borderRadius: 8, border: '1px solid #ffccc7', padding: 16 }}>
      <Alert
        type="error"
        showIcon
        message="沙箱静态检查失败（已自动重试）"
        description={
          <pre style={{ fontSize: 11, margin: 0, whiteSpace: 'pre-wrap', maxHeight: 80, overflow: 'auto' }}>
            {error}
          </pre>
        }
        style={{ marginBottom: 12 }}
      />
      <div style={{ marginBottom: 8, fontWeight: 600, fontSize: 12 }}>
        修改脚本后重新提交：
      </div>
      <Input.TextArea
        value={editedScript}
        onChange={e => setEditedScript(e.target.value)}
        rows={12}
        style={{ fontFamily: 'monospace', fontSize: 11, marginBottom: 12 }}
      />
      <Button type="primary" loading={submitting} onClick={handleResubmit}>
        提交修改后的脚本
      </Button>
    </div>
  )
}

// ── MetadataPreviewPanel (inline) ────────────────────────────────────────────

function MetadataPreviewPanel({ taskId }: { taskId: string }): React.ReactElement {
  const [data, setData] = React.useState<{ speakers: Record<string, unknown>[]; total: number } | null>(null)
  const [loading, setLoading] = React.useState(true)

  React.useEffect(() => {
    getMetadataPreview(taskId, 5).then(setData).catch(() => {}).finally(() => setLoading(false))
  }, [taskId])

  if (loading) return <Spin size="small" />
  if (!data || data.speakers.length === 0) return <Typography.Text type="secondary">暂无数据</Typography.Text>

  return (
    <div>
      <Typography.Text type="secondary" style={{ fontSize: 11 }}>
        共 {data.total} 个说话人，展示前 {data.speakers.length} 条
      </Typography.Text>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 }}>
        {data.speakers.map((sp, i) => (
          <div key={i} style={{ background: '#fafafa', border: '1px solid #f0f0f0', borderRadius: 6, padding: '8px 12px', fontSize: 11 }}>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 4 }}>
              <Tag color="blue">{String(sp.speaker_name ?? sp.speaker ?? '')}</Tag>
              {sp.gender ? <Tag color={(sp.gender as string) === '男' ? 'geekblue' : 'pink'}>{String(sp.gender)}</Tag> : null}
              {sp.accent && (sp.accent as string) !== 'mandarin' ? <Tag color="orange">{String(sp.accent)}</Tag> : null}
              {sp.actor ? <Tag>{String(sp.actor)}</Tag> : null}
            </div>
            <div style={{ display: 'flex', gap: 16, color: '#595959', flexWrap: 'wrap' }}>
              <span>采样率 {sp.sample_rate as number}Hz</span>
              <span>格式 {sp.format as string}</span>
              <span>{sp.count as number} 条</span>
              <span>{((sp.duration as number) * 60).toFixed(0)}min</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── AsrPreviewPanel (inline) ──────────────────────────────────────────────────

function AsrPreviewPanel({ taskId }: { taskId: string }): React.ReactElement {
  const [data, setData] = React.useState<{ rows: Record<string, unknown>[]; total_rows: number; total_speakers: number } | null>(null)
  const [loading, setLoading] = React.useState(true)

  React.useEffect(() => {
    getAsrPreview(taskId, 15).then(setData).catch(() => {}).finally(() => setLoading(false))
  }, [taskId])

  if (loading) return <Spin size="small" />
  if (!data || data.rows.length === 0) return <Typography.Text type="secondary">暂无数据</Typography.Text>

  const columns = [
    { title: 'key', dataIndex: 'key', key: 'key', width: 180,
      render: (v: string) => <Typography.Text code style={{ fontSize: 10 }}>{v}</Typography.Text> },
    { title: 'ASR 文本', dataIndex: 'text', key: 'text',
      render: (v: string) => <span style={{ fontSize: 12 }}>{v || <Typography.Text type="secondary">（空）</Typography.Text>}</span> },
    { title: 'dur', dataIndex: 'duration', key: 'duration', width: 55,
      render: (v: number) => <span style={{ fontSize: 11 }}>{v?.toFixed(1)}s</span> },
  ]

  return (
    <div>
      <Typography.Text type="secondary" style={{ fontSize: 11 }}>
        共 {data.total_speakers} 个说话人，{data.total_rows} 条记录，展示前 {data.rows.length} 条
      </Typography.Text>
      <Table
        dataSource={data.rows.map((r, i) => ({ ...r, _key: i }))}
        rowKey="_key"
        columns={columns}
        size="small"
        pagination={false}
        style={{ marginTop: 8, fontSize: 11 }}
        scroll={{ y: 300 }}
      />
    </div>
  )
}

// ── Main page component ───────────────────────────────────────────────────────

const TASK_STORAGE_KEY = 'dataprocess_task_id'

export default function DataProcess(): React.ReactElement {
  // Restore taskId from localStorage on mount (survives navigation)
  const [pageState, setPageState] = useState<PageState>(() => {
    const saved = localStorage.getItem(TASK_STORAGE_KEY)
    return saved ? 'executing' : 'form'
  })
  const [taskId, setTaskId] = useState<string | null>(() =>
    localStorage.getItem(TASK_STORAGE_KEY),
  )
  const [currentPause, setCurrentPause] = useState<SSEEvent | null>(null)
  const [doneSummary, setDoneSummary] = useState<DoneSummary | null>(null)
  const [completedSteps, setCompletedSteps] = useState<string[]>([])
  const [reviewSubmitted, setReviewSubmitted] = useState(false)

  // History: maps ALL_STEPS key → last meaningful pause event for that step
  const [pauseHistory, setPauseHistory] = useState<Record<string, SSEEvent>>({})
  // stepDoneHistory: maps ALL_STEPS key → step_done summary text (for non-pause steps)
  const [stepDoneHistory, setStepDoneHistory] = useState<Record<string, string>>({})
  const [selectedHistoryStep, setSelectedHistoryStep] = useState<string | null>(null)

  // currentStep derived from the latest pause event's step
  const currentStep = currentPause?.step ?? null

  /** Map a pause step name to the ALL_STEPS key (for progress indicator). */
  function pauseStepToAllStep(step: string): string | null {
    if (['classify_review', 'classify_confirm', 'sandbox_error', 'sandbox_fix'].includes(step)) return 'classify'
    if (step === 'metadata') return 'metadata'
    if (['preview_silence_trim', 'silence_trim_review'].includes(step)) return 'silence_trim'
    if (['segment_confirm', 'segment_done'].includes(step)) return 'segment'
    if (step === 'asr') return 'asr'
    if (['preview_pause_compress', 'pause_compress_review'].includes(step)) return 'pause_compress'
    if (['preview_gain', 'gain_review'].includes(step)) return 'gain'
    if (step === 'flag_short') return 'flag_short'
    if (step === 'flag_richtext') return 'flag_richtext'
    if (step === 'flag_short_result') return 'flag_short'
    if (step === 'flag_richtext_result') return 'flag_richtext'
    if (step === 'review_session') return 'review'
    return null
  }

  /** Map a pause step to the history key for storing result panels. */
  function pauseStepToHistoryKey(step: string): string | null {
    if (step === 'classify_confirm') return 'classify'
    if (step === 'silence_trim_review') return 'silence_trim'
    if (step === 'segment_done') return 'segment'
    if (step === 'pause_compress_review') return 'pause_compress'
    if (step === 'gain_review') return 'gain'
    if (step === 'flag_short_result') return 'flag_short'
    if (step === 'flag_richtext_result') return 'flag_richtext'
    if (step.startsWith('preview_')) {
      const s = step.slice(8)
      if (['pause_compress', 'gain'].includes(s)) return s
    }
    return null
  }

  // ── Dismiss the current pause panel ──────────────────────────────────────

  const dismissCurrentPause = useCallback(() => {
    if (currentPause?.step) {
      const allKey = pauseStepToAllStep(currentPause.step) ?? currentPause.step
      setCompletedSteps((prev) => prev.includes(allKey) ? prev : [...prev, allKey])
    }
    setCurrentPause(null)
  }, [currentPause])

  // ── State transition handlers ────────────────────────────────────────────

  const handleTaskCreated = useCallback((id: string) => {
    localStorage.setItem(TASK_STORAGE_KEY, id)
    localStorage.setItem(`task_start_ms_${id}`, Date.now().toString())
    setTaskId(id)
    setPageState('executing')
    setCompletedSteps([])
    setCurrentPause(null)
    setDoneSummary(null)
    setReviewSubmitted(false)
    setLastStep(null)
  }, [])

  const handlePause = useCallback((event: SSEEvent) => {
    if (event.step === 'review_session') {
      setCurrentPause(null)
      setPageState('review')
      // Mark all prior steps as completed
      setCompletedSteps(prev => {
        const newCompleted = [...prev]
        for (const s of ALL_STEPS) {
          if (s !== 'review' && !newCompleted.includes(s)) newCompleted.push(s)
        }
        return newCompleted
      })
      return
    }
    setCurrentPause(event)
    setSelectedHistoryStep(null)
    // Track progress in indicator
    const allKey = pauseStepToAllStep(event.step ?? '')
    if (allKey && allKey !== 'review') {
      // Mark all earlier steps as completed
      const idx = ALL_STEPS.indexOf(allKey)
      if (idx > 0) {
        setCompletedSteps(prev => {
          const next = [...prev]
          for (let i = 0; i < idx; i++) {
            if (!next.includes(ALL_STEPS[i])) next.push(ALL_STEPS[i])
          }
          return next
        })
      }
    }
    // Store in history
    const hKey = pauseStepToHistoryKey(event.step ?? '')
    if (hKey) {
      setPauseHistory(prev => ({ ...prev, [hKey]: event }))
    }
  }, [])

  const handleDone = useCallback((event: SSEEvent) => {
    const summary =
      (event.summary as unknown as DoneSummary) ||
      (event.payload as unknown as DoneSummary) ||
      null
    setDoneSummary(summary)
    setCurrentPause(null)
    setPageState('done')
    localStorage.removeItem(TASK_STORAGE_KEY)  // task finished, clear persistence
  }, [])

  const [lastStep, setLastStep] = useState<{ step: string; summary: string } | null>(null)

  const handleError = useCallback((_event: SSEEvent) => {
    // Errors are shown in the left log panel only; right panel is not affected
  }, [])

  const handleStepDone = useCallback((step: string, summary: string) => {
    setLastStep({ step, summary })
    // Mark non-pause steps as completed
    const autoComplete = ['metadata', 'asr', 'flag_short', 'flag_richtext',
                          'silence_trim', 'segment', 'pause_compress', 'gain']
    if (autoComplete.includes(step)) {
      setCompletedSteps(prev => prev.includes(step) ? prev : [...prev, step])
    }
    // Store summary for history panel (text-only steps: metadata, asr only)
    const textHistorySteps = ['metadata', 'asr']
    if (textHistorySteps.includes(step) && summary) {
      setStepDoneHistory(prev => ({ ...prev, [step]: summary }))
    }
  }, [])

  const handleReviewComplete = useCallback(() => {
    // All review batches already submitted via submit_review.
    // Send approve to unblock _phase_done (sets task.status back to pending).
    setReviewSubmitted(true)
    submitAction(taskId!, { action: 'approve', step: 'review_session' })
      .catch((e) => console.error('review approve failed:', e))
    message.success('人工校对已提交，等待后台处理完成...')
  }, [taskId])

  const handleStartNew = useCallback(async () => {
    // Cancel the current task on backend before resetting frontend state
    const savedId = localStorage.getItem(TASK_STORAGE_KEY)
    if (savedId) {
      try { await cancelTask(savedId) } catch { /* ignore */ }
      localStorage.removeItem(`task_start_ms_${savedId}`)
    }
    localStorage.removeItem(TASK_STORAGE_KEY)
    setTaskId(null)
    setCurrentPause(null)
    setDoneSummary(null)
    setCompletedSteps([])
    setReviewSubmitted(false)
    setPauseHistory({})
    setStepDoneHistory({})
    setSelectedHistoryStep(null)
    setPageState('form')
  }, [])

  // ── Action panel routing ────────────────────────────────────────────────

  const renderActionPanel = (pauseEvent?: SSEEvent | null, isHistory = false): React.ReactNode => {
    const evt = pauseEvent ?? currentPause
    if (!evt || !taskId) return null

    const step = evt.step || ''
    const payload = (evt.payload || {}) as Record<string, unknown>

    // classify_confirm → ClassifyConfirmPanel
    if (step === 'classify_confirm') {
      const dirTree = payload.dir_tree as unknown
      const scriptPreview = (payload.script_preview as string) || ''
      const mergeCandidates: MergeCandidate[] = Array.isArray(
        payload.merge_candidates,
      )
        ? (payload.merge_candidates as MergeCandidate[])
        : []
      const integrity = (payload.integrity as Record<string, unknown>) || {}
      const integrityOk = !!(integrity.ok ?? true)
      const integrityDetail = (integrity.detail as string) || ''

      return (
        <ClassifyConfirmPanel
          taskId={taskId}
          dirTree={dirTree}
          scriptPreview={scriptPreview}
          mergeCandidates={mergeCandidates}
          integrityOk={integrityOk}
          integrityDetail={integrityDetail}
          onAction={async (action, opts) => {
            try {
              await submitAction(taskId, {
                action,
                step,
                feedback: opts?.feedback,
                merge_tasks: opts?.confirmedMerges?.map((m) => ({
                  src_speaker: m.src_speaker,
                  dst_speaker: m.dst_speaker,
                })),
                save_experience: opts?.saveExperience ?? false,
              })
              dismissCurrentPause()
            } catch {
              message.error('操作失败')
            }
          }}
        />
      )
    }

    // Audio processing steps → PreviewConfirmPanel
    // Executor emits pause as "preview_{step_name}" (e.g. "preview_silence_trim").
    // Strip the prefix to check which audio step is involved.
    const audioStep = step.startsWith('preview_') ? step.slice(8) : step
    if (
      ['silence_trim', 'pause_compress', 'gain', 'segment',
       'silence_trim_review', 'pause_compress_review', 'gain_review'].includes(audioStep)
    ) {
      const groups = Array.isArray(payload.groups)
        // Normalise field names: SSE payload uses "representative_params",
        // PreviewConfirmPanel expects "params".
        ? (payload.groups as any[]).map((g) => ({
            ...g,
            params: g.params || g.representative_params || {},
          }))
        : []

      return (
        <PreviewConfirmPanel
          taskId={taskId}
          step={audioStep}
          groups={groups as any[]}
          onAction={async (action, payload) => {
            try {
              await submitAction(taskId, {
                action,
                step: audioStep,
                feedback: payload ? JSON.stringify(payload) : undefined,
              })
              dismissCurrentPause()
            } catch {
              message.error('操作失败')
            }
          }}
        />
      )
    }

    // flag_short_result / flag_richtext_result → show flagged count + samples
    if (step === 'flag_short_result' || step === 'flag_richtext_result') {
      const totalFlagged = payload.total_flagged as number ?? 0
      const samples = (payload.samples as any[]) ?? []
      const label = payload.label as string ?? (step === 'flag_short_result' ? '过短片段' : '富文本')
      const isHistory = false  // always actionable when rendered live
      return (
        <div style={{ background: '#fff', borderRadius: 8, border: '1px solid #d9d9d9', padding: 16 }}>
          <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 12 }}>
            {step === 'flag_short_result' ? '🔤 过短标注完成' : '📝 富文本标注完成'}
          </div>
          <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 16 }}>
            <div style={{ background: totalFlagged > 0 ? '#fff7e6' : '#f6ffed', border: `1px solid ${totalFlagged > 0 ? '#ffd591' : '#b7eb8f'}`, borderRadius: 8, padding: '10px 20px', textAlign: 'center' }}>
              <div style={{ fontSize: 24, fontWeight: 700, color: totalFlagged > 0 ? '#fa8c16' : '#52c41a' }}>{totalFlagged}</div>
              <div style={{ fontSize: 11, color: '#8c8c8c', marginTop: 2 }}>标注为{label}</div>
            </div>
            {totalFlagged === 0 && <span style={{ fontSize: 12, color: '#52c41a' }}>✓ 未检测到{label}，数据质量良好</span>}
          </div>
          {samples.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 6 }}>样本（共 {samples.length} 条，最多展示 10 条）：</div>
              <div style={{ maxHeight: 200, overflow: 'auto', border: '1px solid #f0f0f0', borderRadius: 4 }}>
                {samples.map((s: any, idx: number) => (
                  <div key={idx} style={{ padding: '6px 10px', borderBottom: '1px solid #f5f5f5', fontSize: 11 }}>
                    <span style={{ color: '#8c8c8c', marginRight: 8 }}>{s.speaker}</span>
                    <span style={{ color: '#1677ff', marginRight: 8, fontFamily: 'monospace' }}>{s.key}</span>
                    <span style={{ color: '#333' }}>{s.text || '（无文本）'}</span>
                    {s.duration != null && <span style={{ color: '#bbb', marginLeft: 8 }}>{(s.duration as number).toFixed(1)}s</span>}
                  </div>
                ))}
              </div>
            </div>
          )}
          <Button type="primary" onClick={async () => {
            try {
              await submitAction(taskId, { action: 'approve', step })
              dismissCurrentPause()
            } catch { message.error('操作失败') }
          }}>
            确认，继续
          </Button>
        </div>
      )
    }

    // segment_done → show results and continue to ASR
    if (step === 'segment_done') {
      const filesSplit = payload.files_split as number ?? 0
      const segmentsCreated = payload.segments_created as number ?? 0
      const filesScanned = payload.files_scanned as number ?? 0
      const thresholdSec = payload.threshold_sec as number ?? 2.0
      const summary = payload.summary as string ?? ''
      return (
        <div style={{ background: '#fff', borderRadius: 8, border: '1px solid #d9d9d9', padding: 16 }}>
          <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 12 }}>✂️ 切句完成</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
            {[
              { label: '扫描文件', value: filesScanned },
              { label: `被切分（停顿 > ${thresholdSec}s）`, value: filesSplit },
              { label: '新增切分段', value: Math.max(0, segmentsCreated - filesSplit) },
            ].map(({ label, value }) => (
              <div key={label} style={{ background: '#f6f6f6', borderRadius: 6, padding: '10px 14px', textAlign: 'center' }}>
                <div style={{ fontSize: 22, fontWeight: 700, color: value > 0 ? '#1677ff' : '#333' }}>{value}</div>
                <div style={{ fontSize: 11, color: '#8c8c8c', marginTop: 2 }}>{label}</div>
              </div>
            ))}
          </div>
          {summary && <div style={{ fontSize: 12, color: '#595959', marginBottom: 16 }}>{summary}</div>}
          <Button type="primary" onClick={async () => {
            try {
              await submitAction(taskId, { action: 'approve', step: 'segment_done' })
              dismissCurrentPause()
            } catch { message.error('操作失败') }
          }}>
            继续（进入 ASR 转写）
          </Button>
        </div>
      )
    }

    // segment_confirm → simple threshold input panel
    if (step === 'segment_confirm') {
      return (
        <SegmentConfirmPanel
          taskId={taskId}
          defaultThreshold={(payload.threshold_sec as number) ?? 2.0}
          onDismiss={dismissCurrentPause}
        />
      )
    }

    // classify_review → show script preview for user to review before execution
    if (step === 'classify_review') {
      const scriptPreview = (payload.script_preview as string) || '（脚本内容为空）'
      return (
        <div style={{ background: '#fff', borderRadius: 8, border: '1px solid #d9d9d9', padding: 16 }}>
          <div style={{ marginBottom: 12, fontWeight: 600, fontSize: 13 }}>
            🤖 LLM 已生成分类脚本，请审核后确认执行
          </div>
          <div style={{
            background: '#1e1e1e', color: '#d4d4d4', borderRadius: 6,
            padding: '10px 12px', fontSize: 12, fontFamily: 'monospace',
            maxHeight: 320, overflow: 'auto', marginBottom: 16, whiteSpace: 'pre-wrap'
          }}>
            {scriptPreview}
          </div>
          <GenericApprovePanel taskId={taskId} step={step} onDismiss={dismissCurrentPause} />
        </div>
      )
    }

    // sandbox_error → show error + editable script + dev bypass option
    if (step === 'sandbox_error') {
      const sandboxError = (payload.error as string) || '未知沙箱错误'
      const failedScript = (payload.script as string) || ''
      return (
        <SandboxErrorPanel
          taskId={taskId}
          error={sandboxError}
          script={failedScript}
          onDismiss={dismissCurrentPause}
        />
      )
    }

    // dataset_preview → DatasetBrowser (file browser + audio preview)
    if (step === 'dataset_preview') {
      const structureType = (payload.structure_type as string) || 'flat'
      const totalFiles = (payload.total_audio_files as number) || 0
      return (
        <DatasetBrowser
          taskId={taskId}
          structureType={structureType}
          totalFiles={totalFiles}
          onConfirm={async (description) => {
            try {
              await submitAction(taskId, {
                action: 'approve',
                step: 'dataset_preview',
                feedback: description,
              })
              dismissCurrentPause()
            } catch {
              message.error('操作失败')
            }
          }}
          onSkip={async () => {
            try {
              await submitAction(taskId, {
                action: 'approve',
                step: 'dataset_preview',
                feedback: '',
              })
              dismissCurrentPause()
            } catch {
              message.error('操作失败')
            }
          }}
        />
      )
    }

    // Fallback: any other pause → GenericApprovePanel (not shown in history view)
    if (isHistory) return null
    return (
      <GenericApprovePanel
        taskId={taskId}
        step={step}
        onDismiss={dismissCurrentPause}
      />
    )
  }

  // ── Derived values ─────────────────────────────────────────────────────────

  const isRunning = pageState === 'executing' && currentPause === null
  const hasTask =
    taskId !== null &&
    (pageState === 'executing' || pageState === 'review' || pageState === 'done')

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div style={{ height: 'calc(100vh - 48px)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* ── form state — full width, centered ───────────────────────────── */}
      {pageState === 'form' && (
        <div style={{ padding: 24, maxWidth: 960, margin: '0 auto' }}>
          <NewIngestForm onTaskCreated={handleTaskCreated} />
        </div>
      )}

      {/* ── executing / review / done — left/right split layout ────────── */}
      {hasTask && (
        <div style={{ display: 'flex', flex: 1, overflow: 'hidden', minHeight: 0 }}>
          {/* ── Left: Agent timeline (always visible during executing+) ── */}
          <div style={{ width: 280, minWidth: 280, flexShrink: 0, display: 'flex', flexDirection: 'column', borderRight: '1px solid #f0f0f0', background: '#fafafa' }}>
            <AgentLogStream
              taskId={taskId!}
              isRunning={isRunning}
              onPause={handlePause}
              onDone={handleDone}
              onError={handleError}
              onStepDone={handleStepDone}
            />
          </div>

          {/* ── Right: content area ─────────────────────────────────────── */}
          <div style={{ flex: 1, minWidth: 0, overflow: 'auto', padding: '16px 24px' }}>
            {/* ── executing ─────────────────────────────────────────────── */}
            {pageState === 'executing' && (
              <>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 8 }}>
                  <div style={{ flex: 1 }}>
                    <StepIndicator
                      steps={ALL_STEPS}
                      currentStep={selectedHistoryStep
                        ? null
                        : (pauseStepToAllStep(currentStep ?? '') ?? null)}
                      completedSteps={completedSteps}
                      historySteps={[...new Set([...Object.keys(pauseHistory), ...Object.keys(stepDoneHistory)])]}
                      selectedHistoryStep={selectedHistoryStep}
                      onStepClick={(step) => {
                        setSelectedHistoryStep(prev => prev === step ? null : step)
                      }}
                    />
                  </div>
                  <Button size="small" danger onClick={handleStartNew} style={{ marginTop: 4, flexShrink: 0 }}>
                    终止任务
                  </Button>
                </div>
                {/* Right panel: history OR current pause OR spinner (mutually exclusive) */}
                {selectedHistoryStep && (pauseHistory[selectedHistoryStep] || stepDoneHistory[selectedHistoryStep]) ? (
                  /* ── History view ─────────────────────────────────────── */
                  <div style={{ marginTop: 16 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, padding: '6px 10px', background: '#f0f5ff', borderRadius: 6, border: '1px solid #adc6ff' }}>
                      <span style={{ fontSize: 12, color: '#2f54eb' }}>
                        📋 历史结果：{selectedHistoryStep}
                      </span>
                      <Button size="small" type="link" style={{ marginLeft: 'auto', padding: 0 }}
                        onClick={() => setSelectedHistoryStep(null)}>
                        返回当前步骤 →
                      </Button>
                    </div>
                    {pauseHistory[selectedHistoryStep]
                      ? renderActionPanel(pauseHistory[selectedHistoryStep], true)
                      : selectedHistoryStep === 'metadata' && taskId ? (
                        <div>
                          <div style={{ fontSize: 11, color: '#595959', marginBottom: 8 }}>{stepDoneHistory['metadata']}</div>
                          <MetadataPreviewPanel taskId={taskId} />
                        </div>
                      ) : selectedHistoryStep === 'asr' && taskId ? (
                        <div>
                          <div style={{ fontSize: 11, color: '#595959', marginBottom: 8 }}>{stepDoneHistory['asr']}</div>
                          <AsrPreviewPanel taskId={taskId} />
                        </div>
                      ) : (
                        <div style={{ background: '#f6ffed', border: '1px solid #b7eb8f', borderRadius: 8, padding: '12px 16px' }}>
                          <div style={{ fontSize: 12, color: '#389e0d', fontWeight: 600, marginBottom: 4 }}>
                            ✓ {selectedHistoryStep} 完成
                          </div>
                          <div style={{ fontSize: 11, color: '#595959' }}>
                            {stepDoneHistory[selectedHistoryStep]}
                          </div>
                        </div>
                      )
                    }
                  </div>
                ) : currentPause ? (
                  /* ── Current action panel ─────────────────────────────── */
                  <div style={{ marginTop: 16 }}>{renderActionPanel()}</div>
                ) : (
                  /* ── Executing / waiting ──────────────────────────────── */
                  <div style={{ textAlign: 'center', padding: '40px 0' }}>
                    <Spin size="large" />
                    <div style={{ marginTop: 12, color: '#8c8c8c', fontSize: 13 }}>
                      Agent 处理中，详情见左侧流水…
                    </div>
                  </div>
                )}
              </>
            )}

            {/* ── review ────────────────────────────────────────────────── */}
            {pageState === 'review' && (
              <>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 8 }}>
                  <div style={{ flex: 1 }}>
                    <StepIndicator
                      steps={ALL_STEPS}
                      currentStep="review"
                      completedSteps={completedSteps}
                      historySteps={Object.keys(pauseHistory)}
                      selectedHistoryStep={selectedHistoryStep}
                      onStepClick={(step) => setSelectedHistoryStep(prev => prev === step ? null : step)}
                    />
                  </div>
                  <Button size="small" danger onClick={handleStartNew}>终止任务</Button>
                </div>
                {selectedHistoryStep && (pauseHistory[selectedHistoryStep] || stepDoneHistory[selectedHistoryStep]) && (
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, padding: '6px 10px', background: '#f0f5ff', borderRadius: 6, border: '1px solid #adc6ff' }}>
                      <span style={{ fontSize: 12, color: '#2f54eb' }}>📋 查看历史：{selectedHistoryStep}</span>
                      <Button size="small" type="link" style={{ marginLeft: 'auto', padding: 0 }}
                        onClick={() => setSelectedHistoryStep(null)}>
                        返回人工校对 →
                      </Button>
                    </div>
                    {pauseHistory[selectedHistoryStep]
                      ? renderActionPanel(pauseHistory[selectedHistoryStep], true)
                      : (
                        <div style={{ background: '#f6ffed', border: '1px solid #b7eb8f', borderRadius: 8, padding: '12px 16px' }}>
                          <div style={{ fontSize: 12, color: '#389e0d', fontWeight: 600, marginBottom: 4 }}>✓ {selectedHistoryStep} 完成</div>
                          <div style={{ fontSize: 11, color: '#595959' }}>{stepDoneHistory[selectedHistoryStep]}</div>
                        </div>
                      )
                    }
                  </div>
                )}
                {!selectedHistoryStep && (
                  !reviewSubmitted ? (
                    <ReviewPlayer taskId={taskId!} onComplete={handleReviewComplete} />
                  ) : (
                    <div style={{ padding: 64, textAlign: 'center' }}>
                      <Spin size="large" tip="正在等待后台处理完成..." />
                    </div>
                  )
                )}
              </>
            )}

            {/* ── done ──────────────────────────────────────────────────── */}
            {pageState === 'done' && taskId && doneSummary && (
              <div>
                <DonePanel taskId={taskId} summary={doneSummary} />
                <div style={{ textAlign: 'center', marginTop: 32 }}>
                  <Button type="primary" onClick={handleStartNew}>
                    开始新任务
                  </Button>
                </div>
              </div>
            )}

            {pageState === 'done' && taskId && !doneSummary && (
              <div style={{ textAlign: 'center', padding: 64 }}>
                <Spin size="large" tip="处理完成，正在加载摘要..." />
                <div style={{ marginTop: 24 }}>
                  <Button type="primary" onClick={handleStartNew}>
                    开始新任务
                  </Button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
