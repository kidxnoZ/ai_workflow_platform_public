import { useState, useEffect, useRef, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Button, Slider, Checkbox, Input, Card, Tag,
  message, Spin, Alert, Typography, Progress, Tooltip,
} from 'antd'
import {
  PlayCircleOutlined, PauseCircleOutlined, ArrowLeftOutlined,
  ArrowRightOutlined, LogoutOutlined, CheckCircleOutlined,
} from '@ant-design/icons'

const { Title, Text } = Typography

const ISSUE_TAGS = ['相似度低', '不自然', '情感平淡', '情感过度', '语速不对', '有词错误', '停顿异常']

interface Sample {
  id: string
  key: string
  model: string
  speaker: string
  speaker_type: string
  test_set: string
  audio_url: string | null
  ref_audio_url: string | null
  wer_score: number | null
  sim_score: number | null
}

interface Group {
  id: string
  text: string
  eval_count: number
  samples: Sample[]
  metrics: string[]
}

interface SampleRatingInput {
  sample_id: string
  mos_score: number | null
  nmos_score: number | null
  smos_score: number | null
  issue_tags: string[]
  notes: string
}

// ── Audio Player ──────────────────────────────────────────────────────────────

function AudioPlayer({ url }: { url: string }) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [progress, setProgress] = useState(0)

  const toggle = () => {
    const a = audioRef.current
    if (!a) return
    if (playing) {
      a.pause()
    } else {
      document.querySelectorAll('audio').forEach((el) => { if (el !== a) el.pause() })
      a.play()
    }
  }

  useEffect(() => {
    const a = audioRef.current
    if (!a) return
    const onPlay = () => setPlaying(true)
    const onPause = () => setPlaying(false)
    const onEnded = () => { setPlaying(false); setProgress(0) }
    const onTime = () => { if (a.duration) setProgress(Math.round((a.currentTime / a.duration) * 100)) }
    a.addEventListener('play', onPlay)
    a.addEventListener('pause', onPause)
    a.addEventListener('ended', onEnded)
    a.addEventListener('timeupdate', onTime)
    return () => {
      a.removeEventListener('play', onPlay)
      a.removeEventListener('pause', onPause)
      a.removeEventListener('ended', onEnded)
      a.removeEventListener('timeupdate', onTime)
    }
  }, [])

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 110, maxWidth: 160 }}>
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <audio ref={audioRef} src={url} preload="none" />
      <Button
        type={playing ? 'primary' : 'default'}
        shape="circle"
        size="small"
        icon={playing ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
        onClick={toggle}
      />
      <Progress percent={progress} size="small" showInfo={false} style={{ flex: 1 }} />
    </div>
  )
}

// ── Score Slider ──────────────────────────────────────────────────────────────

function ScoreSlider({ value, onChange }: { value: number | null; onChange: (v: number) => void }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 8px 4px 14px' }}>
      <Slider
        min={0} max={5} step={0.1}
        value={value ?? 0}
        onChange={(v) => onChange(v as number)}
        tooltip={{ formatter: (v) => v != null ? Number(v).toFixed(1) : '—' }}
        marks={{
          0: { style: { fontSize: 9 }, label: '0' },
          2.5: { style: { fontSize: 9 }, label: '2.5' },
          5: { style: { fontSize: 9 }, label: '5' },
        }}
        style={{ flex: 1, margin: 0 }}
      />
      <Text strong style={{ fontSize: 12, color: value !== null ? '#1677ff' : '#d9d9d9', width: 26, textAlign: 'right', flexShrink: 0 }}>
        {value !== null ? value.toFixed(1) : '—'}
      </Text>
    </div>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function shuffleArray<T>(arr: T[]): T[] {
  const a = [...arr]
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]]
  }
  return a
}

function getOrCreateSessionId(): string {
  const key = 'eval_session_id'
  let id = sessionStorage.getItem(key)
  if (!id) { id = crypto.randomUUID(); sessionStorage.setItem(key, id) }
  return id
}

function defaultRating(sampleId: string): SampleRatingInput {
  return { sample_id: sampleId, mos_score: null, nmos_score: null, smos_score: null, issue_tags: [], notes: '' }
}

// ── Session Page ──────────────────────────────────────────────────────────────

export default function OfflineEvalSession() {
  const { taskId } = useParams<{ taskId: string }>()
  const navigate = useNavigate()
  const sessionId = getOrCreateSessionId()

  const [taskInfo, setTaskInfo] = useState<{ name: string; total_groups: number; evaluated_groups: number; metrics: string[] } | null>(null)
  const [group, setGroup] = useState<Group | null>(null)
  const [loading, setLoading] = useState(false)
  const [allVisited, setAllVisited] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  // Draft ratings: group_id → sample_id → rating
  const [draftRatings, setDraftRatings] = useState<Record<string, Record<string, SampleRatingInput>>>({})
  const [shuffledSamples, setShuffledSamples] = useState<Record<string, Sample[]>>({})
  // Visited group history for prev/next
  const [groupHistory, setGroupHistory] = useState<Group[]>([])
  const [historyIndex, setHistoryIndex] = useState(-1)

  const showNmos = taskInfo?.metrics.includes('nmos') ?? false
  const showSmos = taskInfo?.metrics.includes('smos') ?? false
  const scoreCols = 1 + (showNmos ? 1 : 0) + (showSmos ? 1 : 0)
  const currentRatings = group ? (draftRatings[group.id] ?? {}) : {}

  const fetchTaskInfo = useCallback(async () => {
    const res = await fetch(`/api/offline-eval/tasks/${taskId}`)
    if (!res.ok) return
    const data = await res.json()
    setTaskInfo({ name: data.name, total_groups: data.total_groups, evaluated_groups: data.evaluated_groups, metrics: data.metrics })
  }, [taskId])

  // Fetch a new group, excluding already-visited ones
  const fetchNextGroup = useCallback(async (visitedIds: string[]) => {
    setLoading(true)
    setError('')
    setAllVisited(false)
    try {
      const exclude = visitedIds.join(',')
      const res = await fetch(`/api/offline-eval/tasks/${taskId}/next-group?exclude=${encodeURIComponent(exclude)}`)
      if (!res.ok) throw new Error('获取评价组失败')
      const data = await res.json()
      if (!data.group) {
        setAllVisited(true)
        setGroup(null)
      } else {
        const g: Group = data.group
        setGroup(g)
        setShuffledSamples(prev => prev[g.id] ? prev : { ...prev, [g.id]: shuffleArray(g.samples) })
        setDraftRatings(prev => {
          if (prev[g.id]) return prev
          const init: Record<string, SampleRatingInput> = {}
          for (const s of g.samples) init[s.id] = defaultRating(s.id)
          return { ...prev, [g.id]: init }
        })
        setGroupHistory(prev => {
          if (prev.some(existing => existing.id === g.id)) return prev
          return [...prev, g]
        })
        setHistoryIndex(visitedIds.length)
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [taskId])

  useEffect(() => {
    fetchTaskInfo()
    fetchNextGroup([])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleRatingChange = (sampleId: string, update: Partial<SampleRatingInput>) => {
    if (!group) return
    setDraftRatings(prev => ({
      ...prev,
      [group.id]: { ...prev[group.id], [sampleId]: { ...prev[group.id]?.[sampleId] ?? defaultRating(sampleId), ...update } },
    }))
  }

  const handlePrev = () => {
    if (historyIndex <= 0) return
    const idx = historyIndex - 1
    setHistoryIndex(idx)
    setGroup(groupHistory[idx])
    setAllVisited(false)
  }

  const handleNext = () => {
    if (historyIndex < groupHistory.length - 1) {
      // Already visited — just advance
      const idx = historyIndex + 1
      setHistoryIndex(idx)
      setGroup(groupHistory[idx])
      setAllVisited(false)
    } else {
      // Fetch a new group, excluding all visited
      fetchNextGroup(groupHistory.map(g => g.id))
    }
  }

  const handleSubmitAll = async () => {
    const groupsToSubmit: Array<{ group_id: string; ratings: SampleRatingInput[] }> = []
    for (const [gId, samples] of Object.entries(draftRatings)) {
      const ratings = Object.values(samples)
      if (ratings.some(r => r.mos_score !== null)) {
        groupsToSubmit.push({ group_id: gId, ratings })
      }
    }
    if (groupsToSubmit.length === 0) { message.warning('没有可提交的评分，请先打分'); return }

    const incomplete = groupsToSubmit.filter(g => g.ratings.some(r => r.mos_score === null))
    if (incomplete.length > 0) { message.warning('部分组还有未评总体 MOS 的样本'); return }
    if (showNmos && groupsToSubmit.some(g => g.ratings.some(r => r.nmos_score === null))) {
      message.warning('部分组还有未评自然度 NMOS 的样本'); return
    }
    if (showSmos && groupsToSubmit.some(g => g.ratings.some(r => r.smos_score === null))) {
      message.warning('部分组还有未评相似度 SMOS 的样本'); return
    }

    setSubmitting(true)
    try {
      const res = await fetch(`/api/offline-eval/tasks/${taskId}/submit-batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, groups: groupsToSubmit }),
      })
      if (!res.ok) throw new Error('提交失败')
      message.success(`成功提交 ${groupsToSubmit.length} 组评分`)
      setDraftRatings({})
      setGroupHistory([])
      setHistoryIndex(-1)
      setAllVisited(false)
      await fetchTaskInfo()
      fetchNextGroup([])
    } catch (e) {
      message.error((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  // ── Render States ─────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 60 }}>
        <Spin size="large" />
        <div style={{ marginTop: 16, color: '#8c8c8c' }}>加载评价组...</div>
      </div>
    )
  }

  if (error) {
    return <Alert type="error" message={error} action={<Button onClick={() => fetchNextGroup([])}>重试</Button>} />
  }

  if (allVisited && !group) {
    const draftCount = Object.values(draftRatings).filter(s => Object.values(s).some(r => r.mos_score !== null)).length
    return (
      <div style={{ textAlign: 'center', padding: 60 }}>
        <Title level={5} style={{ color: '#595959' }}>已浏览所有组</Title>
        <Text type="secondary">共浏览 {groupHistory.length} 组，待提交 {draftCount} 组</Text>
        <div style={{ marginTop: 24, display: 'flex', gap: 12, justifyContent: 'center' }}>
          {groupHistory.length > 0 && (
            <Button icon={<ArrowLeftOutlined />} onClick={handlePrev}>返回上一组</Button>
          )}
          <Button type="primary" icon={<CheckCircleOutlined />} onClick={handleSubmitAll} loading={submitting}>
            提交全部 ({draftCount})
          </Button>
          <Button onClick={() => navigate('/offline-eval')}>退出</Button>
        </div>
      </div>
    )
  }

  if (!group) return null

  const refAudioUrl = showSmos ? (group.samples.find(s => s.ref_audio_url)?.ref_audio_url ?? null) : null
  const draftCount = Object.values(draftRatings).filter(s => Object.values(s).some(r => r.mos_score !== null)).length

  return (
    <div style={{ paddingRight: 24 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
        <div>
          <Title level={5} style={{ margin: 0 }}>{taskInfo?.name ?? '离线评测'}</Title>
          <Text type="secondary" style={{ fontSize: 12 }}>
            已评 {taskInfo?.evaluated_groups ?? 0} / {taskInfo?.total_groups ?? 0} 组
            {draftCount > 0 && <span style={{ color: '#faad14', marginLeft: 12 }}>待提交 {draftCount} 组</span>}
            <span style={{ marginLeft: 12 }}>当前第 {historyIndex + 1} 组</span>
          </Text>
        </div>
        <Button icon={<LogoutOutlined />} onClick={() => navigate('/offline-eval')}>退出</Button>
      </div>

      {taskInfo && (
        <Progress
          percent={taskInfo.total_groups > 0 ? Math.round((taskInfo.evaluated_groups / taskInfo.total_groups) * 100) : 0}
          size="small"
          style={{ marginBottom: 8 }}
        />
      )}

      {/* Text + Ref Audio in one row */}
      <Card size="small" style={{ marginBottom: 10, background: '#f5f5f5' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
          {refAudioUrl && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
              <Tag color="green" style={{ margin: 0 }}>参考音频</Tag>
              <AudioPlayer url={refAudioUrl} />
            </span>
          )}
          <span>
            <Text strong style={{ fontSize: 13 }}>合成文本：</Text>
            <Text style={{ fontSize: 13 }}>{group.text}</Text>
          </span>
        </div>
      </Card>

      {/* Score Table */}
      <Card size="small" bodyStyle={{ padding: 0 }} style={{ marginBottom: 14 }}>
        {/* Header row */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: `minmax(120px, 0.8fr) repeat(${scoreCols}, minmax(120px, 1fr))`,
          background: '#fafafa',
          borderBottom: '2px solid #e8e8e8',
          borderRadius: '6px 6px 0 0',
          padding: '6px 12px',
        }}>
          <div style={{ textAlign: 'center' }}><Text strong style={{ fontSize: 12, color: '#595959' }}>音频</Text></div>
          <div style={{ textAlign: 'center' }}><Text strong style={{ fontSize: 12, color: '#595959' }}>MOS 总体</Text></div>
          {showNmos && <div style={{ textAlign: 'center' }}><Text strong style={{ fontSize: 12, color: '#595959' }}>NMOS 自然度</Text></div>}
          {showSmos && <div style={{ textAlign: 'center' }}><Text strong style={{ fontSize: 12, color: '#595959' }}>SMOS 相似度</Text></div>}
        </div>

        {/* Sample rows */}
        {(shuffledSamples[group.id] ?? group.samples).map((sample, idx) => {
          const rating = currentRatings[sample.id] ?? defaultRating(sample.id)
          const isLast = idx === group.samples.length - 1
          return (
            <div key={sample.id}>
              {/* Score row */}
              <div style={{
                display: 'grid',
                gridTemplateColumns: `minmax(120px, 0.8fr) repeat(${scoreCols}, minmax(120px, 1fr))`,
                padding: '4px 12px 0',
                alignItems: 'center',
              }}>
                {/* Audio */}
                <div style={{ padding: '4px 0' }}>
                  {sample.audio_url
                    ? <AudioPlayer url={sample.audio_url} />
                    : <Text type="danger" style={{ fontSize: 11 }}>音频未找到</Text>}
                </div>
                {/* MOS */}
                <div style={{ borderLeft: '1px solid #f0f0f0' }}>
                  <ScoreSlider value={rating.mos_score} onChange={v => handleRatingChange(sample.id, { mos_score: v })} />
                </div>
                {/* NMOS */}
                {showNmos && (
                  <div style={{ borderLeft: '1px solid #f0f0f0' }}>
                    <ScoreSlider value={rating.nmos_score} onChange={v => handleRatingChange(sample.id, { nmos_score: v })} />
                  </div>
                )}
                {/* SMOS */}
                {showSmos && (
                  <div style={{ borderLeft: '1px solid #f0f0f0' }}>
                    <ScoreSlider value={rating.smos_score} onChange={v => handleRatingChange(sample.id, { smos_score: v })} />
                  </div>
                )}
              </div>

              {/* Tags + Notes full-width row */}
              <div style={{
                padding: '2px 12px 6px',
                borderBottom: isLast ? 'none' : '1px solid #f0f0f0',
                display: 'flex',
                alignItems: 'center',
                gap: 8,
              }}>
                <Checkbox.Group
                  value={rating.issue_tags}
                  onChange={v => handleRatingChange(sample.id, { issue_tags: v as string[] })}
                  style={{ flex: 1 }}
                >
                  {ISSUE_TAGS.map(tag => (
                    <Checkbox key={tag} value={tag} style={{ fontSize: 11 }}>{tag}</Checkbox>
                  ))}
                </Checkbox.Group>
                <Input
                  size="small"
                  placeholder="备注（可选）"
                  value={rating.notes}
                  onChange={e => handleRatingChange(sample.id, { notes: e.target.value })}
                  style={{ width: 160, fontSize: 12 }}
                />
              </div>
            </div>
          )
        })}
      </Card>

      {/* Navigation */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Button icon={<ArrowLeftOutlined />} onClick={handlePrev} disabled={historyIndex <= 0} size="large">
          上一组
        </Button>
        <Tooltip title={`提交全部已评组（${draftCount} 组）`}>
          <Button
            type="primary"
            icon={<CheckCircleOutlined />}
            onClick={handleSubmitAll}
            loading={submitting}
            size="large"
            disabled={draftCount === 0}
          >
            提交全部 ({draftCount})
          </Button>
        </Tooltip>
        <Button icon={<ArrowRightOutlined />} onClick={handleNext} size="large" disabled={loading}>
          下一组
        </Button>
      </div>
    </div>
  )
}
