/**
 * frontend/src/components/dataset/AgentLogStream.tsx
 *
 * Clone Lab style Agent timeline — left panel of DataProcess.
 * thinking: collapsible purple block (🧠 思考), collapsed by default, 60-char preview
 * step_start: blue progress indicator with live ticking elapsed
 * step_done: green checkmark with summary + elapsed
 * progress: inline progress bar with {current}/{total}
 * error: red alert
 * pause/done: delegated to parent, not rendered inline; flushes thinking first
 *
 * Streams SSE events from /api/dataset-ingest/stream/{taskId}.
 * Elapsed times are relative to task start (first event received).
 */
import React, { useEffect, useRef, useState, useCallback } from 'react'
import { Alert, Progress } from 'antd'
import { LoadingOutlined } from '@ant-design/icons'
import type { SSEEvent } from '../../api/datasetIngest'
import { connectSSE } from '../../api/datasetIngest'

export interface AgentLogStreamProps {
  taskId: string
  isRunning: boolean
  onPause: (event: SSEEvent) => void
  onDone: (event: SSEEvent) => void
  onError: (event: SSEEvent) => void
  onStepDone?: (step: string, summary: string) => void
}

interface LogEntry {
  id: number
  event: SSEEvent
  /** Client-side timestamp when this entry was added (ms since epoch). */
  addedAt: number
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function fmtElapsed(ms: number): string {
  if (ms < 0) return '0s'
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

/** Build a dedup key for an SSE event. Used to skip already-seen events on reconnect.
 *  Thinking events arrive on a separate named channel ("thinking") and are not
 *  deduped here — every chunk is new data. */
function buildDedupKey(ev: SSEEvent): string {
  // Use server timestamp if available (survives navigation replay)
  if (ev.ts) return `${ev.type}:${ev.ts}`
  // Fallback for events without ts
  if (ev.type === 'progress') {
    return `progress:${ev.step ?? ''}:${ev.current ?? 0}:${ev.total ?? 0}`
  }
  return `${ev.type}:${ev.step ?? ''}:${ev.label ?? ''}:${ev.summary ?? ''}:${ev.message ?? ''}`
}

/**
 * Strip fenced code blocks from thinking text, replacing them with a placeholder.
 * Returns {displayText, hasCode} so we can show a note when code was found.
 */
function stripCodeBlocks(text: string): { displayText: string; hasCode: boolean } {
  const codeBlockRe = /```[\s\S]*?```/g
  const hasCode = codeBlockRe.test(text)
  const displayText = text.replace(/```[\s\S]*?```/g, '\n[完整代码见右侧脚本预览]\n').trim()
  return { displayText: displayText || text, hasCode }
}

// ── Component ──────────────────────────────────────────────────────────────────

export default function AgentLogStream({
  taskId,
  isRunning,
  onPause,
  onDone,
  onError,
  onStepDone,
}: AgentLogStreamProps): React.ReactElement {
  const [entries, setEntries] = useState<LogEntry[]>([])
  // Streaming: accumulated thinking text not yet flushed to a regular entry
  const [streamingText, setStreamingText] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  // Collapse state: entry index → expanded
  const [expandedIdx, setExpandedIdx] = useState<Set<number>>(new Set())
  // Scroll tracking
  const [isAtBottom, setIsAtBottom] = useState(true)
  const [lastSeenCount, setLastSeenCount] = useState(0)
  // Live clock for ticking elapsed timers (updated every 1s while running)
  const [nowMs, setNowMs] = useState(Date.now())

  const scrollRef = useRef<HTMLDivElement>(null)
  const nextId = useRef(0)
  const taskStartMs = useRef<number | null>(null)

  // Refs for streaming text so the SSE handler always reads fresh values
  const streamingRef = useRef('')
  const isStreamingRef = useRef(false)
  // Callback refs (stable across renders)
  const onPauseRef = useRef(onPause)
  const onDoneRef = useRef(onDone)
  const onErrorRef = useRef(onError)
  const onStepDoneRef = useRef(onStepDone)
  onPauseRef.current = onPause
  onDoneRef.current = onDone
  onErrorRef.current = onError
  onStepDoneRef.current = onStepDone

  // SSE dedup: skip events that were already processed before a reconnect
  const seenIdsRef = useRef<Set<string>>(new Set())

  // Per-block streaming timer: when the current thinking block started
  const streamStartMsRef = useRef<number | null>(null)

  // Index of the most recent flushed thinking block (for auto-expand)
  const lastThinkingIdxRef = useRef(-1)

  // ── Reset state when taskId changes ─────────────────────────────────────────

  useEffect(() => {
    // Restore persisted start time (set by DataProcess when task was created)
    const saved = localStorage.getItem(`task_start_ms_${taskId}`)
    taskStartMs.current = saved ? parseInt(saved, 10) : null
    setEntries([])
    setStreamingText('')
    setIsStreaming(false)
    streamingRef.current = ''
    isStreamingRef.current = false
    nextId.current = 0
    setExpandedIdx(new Set())
    setIsAtBottom(true)
    setLastSeenCount(0)
    seenIdsRef.current = new Set()
    streamStartMsRef.current = null
    lastThinkingIdxRef.current = -1
  }, [taskId])

  // ── Live clock ──────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!isRunning) return
    const id = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(id)
  }, [isRunning])

  // ── Flush accumulated thinking into a real entry ────────────────────────────

  const flushThinking = useCallback((currentEntries: LogEntry[]): LogEntry[] => {
    if (!isStreamingRef.current || !streamingRef.current) return currentEntries
    const text = streamingRef.current
    streamingRef.current = ''
    isStreamingRef.current = false
    setIsStreaming(false)
    setStreamingText('')
    streamStartMsRef.current = null
    const entry: LogEntry = {
      id: nextId.current++,
      event: { type: 'thinking', content: text },
      addedAt: Date.now(),
    }
    return [...currentEntries, entry]
  }, [])

  // ── SSE event handler ───────────────────────────────────────────────────────
  // IMPORTANT: Callbacks (onPause/onDone/onError) are called OUTSIDE setEntries
  // to avoid side effects inside a React pure updater function.

  const handleEvent = useCallback(
    (ev: SSEEvent) => {
      // Dedup: skip events already seen (happens on SSE reconnect).
      // IMPORTANT: When a structural event is deduplicated, also clear the streaming
      // buffer — thinking tokens replayed from SSE history would otherwise accumulate
      // and mix with new content once the replay finishes.
      const dedupKey = buildDedupKey(ev)
      if (seenIdsRef.current.has(dedupKey)) {
        // Discard any thinking content accumulated from this replayed segment
        if (isStreamingRef.current) {
          streamingRef.current = ''
          isStreamingRef.current = false
          streamStartMsRef.current = null
          setIsStreaming(false)
          setStreamingText('')
        }
        return
      }
      seenIdsRef.current.add(dedupKey)

      // Record task start time from first event's server ts (survives navigation replay)
      if (taskStartMs.current === null) {
        taskStartMs.current = ev.ts ?? Date.now()
      }

      // thinking events arrive on the "thinking" SSE channel (handled by handleThinking)
      // — no need to handle them here.

      // Flush any accumulated thinking text into a LogEntry before adding the
      // structural event (step_start / step_done / progress / error / pause / done).
      const pendingThinkingText = isStreamingRef.current ? streamingRef.current : null
      const pendingThinkingId = pendingThinkingText ? nextId.current++ : -1
      const pendingThinkingAddedAt = ev.ts ?? Date.now()

      streamingRef.current = ''
      isStreamingRef.current = false
      streamStartMsRef.current = null
      setIsStreaming(false)
      setStreamingText('')

      const shouldAddEntry = ev.type !== 'pause' && ev.type !== 'done'
      const newEntryId = shouldAddEntry ? nextId.current++ : -1
      const newEntryAddedAt = ev.ts ?? Date.now()

      setEntries((prev) => {
        const withThinking =
          pendingThinkingText !== null
            ? [
                ...prev,
                {
                  id: pendingThinkingId,
                  event: { type: 'thinking' as const, content: pendingThinkingText },
                  addedAt: pendingThinkingAddedAt,
                },
              ]
            : [...prev]

        if (!shouldAddEntry) return withThinking

        return [
          ...withThinking,
          { id: newEntryId, event: ev, addedAt: newEntryAddedAt },
        ]
      })

      // Callbacks called AFTER setState — side-effect free pattern
      if (ev.type === 'pause') onPauseRef.current(ev)
      else if (ev.type === 'done') onDoneRef.current(ev)
      else if (ev.type === 'error') onErrorRef.current(ev)
      else if (ev.type === 'step_done' && onStepDoneRef.current) {
        onStepDoneRef.current(ev.step ?? '', (ev.summary as string) ?? '')
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )

  // ── Thinking handler (separate SSE channel) ──────────────────────────────────

  const handleThinking = useCallback((content: string) => {
    if (!isStreamingRef.current) {
      streamStartMsRef.current = Date.now()
      if (taskStartMs.current === null) {
        taskStartMs.current = Date.now()
      }
    }
    streamingRef.current += content
    isStreamingRef.current = true
    setIsStreaming(true)
    setStreamingText(streamingRef.current)
  }, [])

  // ── SSE connection ──────────────────────────────────────────────────────────

  useEffect(() => {
    const source = connectSSE(taskId, handleEvent, handleThinking, () => {})
    return () => source.close()
  }, [taskId, handleEvent, handleThinking])

  // ── Scroll management ───────────────────────────────────────────────────────

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 50
    setIsAtBottom(atBottom)
    if (atBottom) setLastSeenCount(entries.length)
  }, [entries.length])

  // Auto-scroll to bottom when new entries or streaming text arrives
  useEffect(() => {
    if (isAtBottom && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
      setLastSeenCount(entries.length)
    }
  }, [entries.length, isAtBottom, streamingText])

  const scrollToBottom = () => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
      setIsAtBottom(true)
      setLastSeenCount(entries.length)
    }
  }

  const unreadCount = entries.length - lastSeenCount

  // ── Toggle thinking block expand/collapse ───────────────────────────────────

  const toggleExpand = (idx: number) => {
    setExpandedIdx((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) {
        next.delete(idx)
      } else if (idx === lastThinkingIdxRef.current && !next.has(-(idx + 1))) {
        // User is collapsing the auto-expanded last thinking block;
        // record negative marker so we don't auto-expand it again.
        next.add(-(idx + 1))
      } else {
        next.add(idx)
      }
      return next
    })
  }

  // ── Event renderers ─────────────────────────────────────────────────────────

  const startMs = taskStartMs.current

  /** Index of the most recent flushed thinking entry (computed on every render). */
  const lastThinkingIdx = entries.reduceRight(
    (found, e, idx) => (found !== -1 ? found : e.event.type === 'thinking' ? idx : -1),
    -1,
  )
  lastThinkingIdxRef.current = lastThinkingIdx

  const renderEntry = (entry: LogEntry, i: number): React.ReactElement | null => {
    const { id, event, addedAt } = entry
    const isLast = i === entries.length - 1

    // Auto-expand the last flushed thinking block (until user explicitly collapses it)
    const isAutoExpanded =
      event.type === 'thinking' &&
      i === lastThinkingIdx &&
      !expandedIdx.has(-(i + 1))
    const isExpanded = expandedIdx.has(i) || isAutoExpanded

    // Live ticking only for the very last entry while running and not streaming
    const isLiveLast = isLast && isRunning && !isStreaming

    // Per-phase elapsed: step_done shows duration from its step_start; step_start live-ticks
    let elapsed: string | null = null
    if (event.type === 'step_done' && event.step) {
      // Find matching step_start in earlier entries
      const matchingStart = [...entries].slice(0, i).reverse()
        .find(e => e.event.type === 'step_start' && e.event.step === event.step)
      if (matchingStart) elapsed = fmtElapsed(Math.max(0, addedAt - matchingStart.addedAt))
    } else if (event.type === 'step_start' && isLiveLast) {
      elapsed = fmtElapsed(Math.max(0, nowMs - addedAt))
    } else if (event.type === 'progress' && isLiveLast) {
      elapsed = fmtElapsed(Math.max(0, nowMs - addedAt))
    }

    switch (event.type) {
      case 'thinking': {
        const rawText = event.content ?? ''
        // Strip code blocks from display — code is shown in full on the right panel
        const { displayText, hasCode } = stripCodeBlocks(rawText)
        const text = displayText
        const preview = text.length > 80 ? text.slice(0, 80) + '…' : text
        return (
          <div
            key={id}
            style={{ marginBottom: 6, cursor: 'pointer' }}
            onClick={() => toggleExpand(i)}
          >
            <div
              style={{
                padding: '4px 8px',
                background: '#f9f0ff',
                borderRadius: 4,
                borderLeft: '3px solid #722ed1',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                }}
              >
                <span
                  style={{ fontSize: 10, color: '#722ed1', fontWeight: 600 }}
                >
                  {'\u{1F9E0}'} 思考{' '}
                  {!isExpanded && (
                    <span style={{ fontWeight: 400, color: '#8c8c8c' }}>
                      {'▸'}
                    </span>
                  )}
                </span>
                {elapsed && (
                  <span
                    style={{
                      fontSize: 10,
                      color: '#d3adf7',
                      fontVariantNumeric: 'tabular-nums',
                    }}
                  >
                    {elapsed}
                  </span>
                )}
              </div>
              {isExpanded ? (
                <div
                  style={{
                    fontSize: 11,
                    color: '#595959',
                    lineHeight: '17px',
                    whiteSpace: 'pre-wrap',
                    marginTop: 2,
                  }}
                >
                  {text}
                </div>
              ) : (
                <div
                  style={{
                    fontSize: 11,
                    color: '#8c8c8c',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {preview}
                </div>
              )}
            </div>
          </div>
        )
      }

      case 'step_start':
        return (
          <div
            key={id}
            style={{
              marginBottom: 4,
              padding: '3px 8px',
              background: '#e6f7ff',
              borderRadius: 4,
              borderLeft: '3px solid #1890ff',
            }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <span style={{ fontSize: 11, fontWeight: 600, color: '#1890ff' }}>
                {'▶'} {event.label ?? event.step}
              </span>
              {elapsed && (
                <span
                  style={{
                    fontSize: 10,
                    color: '#91caff',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {elapsed}
                </span>
              )}
            </div>
          </div>
        )

      case 'step_done':
        return (
          <div
            key={id}
            style={{
              marginBottom: 4,
              padding: '3px 10px',
              background: '#f6ffed',
              borderRadius: 4,
              borderLeft: '3px solid #52c41a',
            }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <span>
                <span
                  style={{ fontSize: 11, color: '#389e0d', fontWeight: 600 }}
                >
                  {'✓'} {event.step}
                </span>
                {event.summary && (
                  <span
                    style={{ fontSize: 10, color: '#8c8c8c', marginLeft: 8 }}
                  >
                    {event.summary as string}
                  </span>
                )}
              </span>
              {elapsed && (
                <span
                  style={{
                    fontSize: 10,
                    color: '#95de64',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {elapsed}
                </span>
              )}
            </div>
          </div>
        )

      case 'progress': {
        const cur = event.current
        const tot = event.total
        const hasBar = cur !== undefined && tot !== undefined && tot > 0
        return (
          <div
            key={id}
            style={{
              marginBottom: 4,
              padding: '3px 10px',
              background: '#fafafa',
              borderRadius: 4,
              borderLeft: '3px solid #d9d9d9',
            }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: hasBar ? 2 : 0,
              }}
            >
              {event.label ? (
                <span style={{ fontSize: 10, color: '#595959' }}>
                  {event.label as string}
                </span>
              ) : (
                <span />
              )}
              {elapsed && (
                <span
                  style={{
                    fontSize: 10,
                    color: '#bfbfbf',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {elapsed}
                </span>
              )}
            </div>
            {hasBar && (
              <Progress
                percent={Math.round((cur! / tot!) * 100)}
                size="small"
                format={() => `${cur}/${tot}`}
              />
            )}
          </div>
        )
      }

      case 'error':
        return (
          <Alert
            key={id}
            type="error"
            message={event.message as string ?? 'Agent 出错'}
            style={{ marginBottom: 4, fontSize: 11 }}
            showIcon
          />
        )

      default:
        return null
    }
  }

  // ── Main render ─────────────────────────────────────────────────────────────

  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        background: '#fafafa',
        minHeight: 0,
        overflow: 'hidden',
      }}
    >
      {/* ── Header ────────────────────────────────────────────────────────── */}
      <div
        style={{
          padding: '12px 12px 8px',
          borderBottom: '1px solid #f0f0f0',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <span
          style={{
            fontSize: 11,
            color: '#8c8c8c',
            letterSpacing: 1,
            fontWeight: 600,
          }}
        >
          Agent 思考过程
        </span>
        <span
          style={{
            fontSize: 11,
            color: '#8c8c8c',
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {isRunning && startMs && fmtElapsed(Math.max(0, nowMs - startMs))}
          {isRunning && (
            <LoadingOutlined style={{ marginLeft: 6, color: '#1890ff' }} />
          )}
          {!isRunning && startMs && entries.length > 0 && (
            <span style={{ color: '#52c41a' }}>
              {'✓'} {fmtElapsed(Math.max(0, entries[entries.length - 1].addedAt - startMs))}
            </span>
          )}
        </span>
      </div>

      {/* ── Scrollable log area ───────────────────────────────────────────── */}
      <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          style={{
            position: 'absolute',
            inset: 0,
            overflowY: 'auto',
            padding: '8px 10px',
          }}
        >
          {/* Empty / waiting states */}
          {entries.length === 0 && !isStreaming &&
            (isRunning ? (
              <div style={{ textAlign: 'center', padding: 20, color: '#bfbfbf' }}>
                <LoadingOutlined style={{ fontSize: 20 }} />
                <br />
                <span style={{ fontSize: 11, color: '#bfbfbf' }}>
                  Agent 启动中…
                </span>
              </div>
            ) : (
              <div
                style={{
                  color: '#bfbfbf',
                  fontSize: 12,
                  textAlign: 'center',
                  paddingTop: 40,
                }}
              >
                等待 Agent 事件…
              </div>
            ))}

          {/* Rendered entries */}
          {entries.map((entry, i) => renderEntry(entry, i))}

          {/* Live streaming thinking block (not yet flushed to a LogEntry) */}
          {isStreaming && startMs && (
            <div style={{ marginBottom: 6 }}>
              <div
                style={{
                  padding: '4px 8px',
                  background: '#f9f0ff',
                  borderRadius: 4,
                  borderLeft: '3px solid #722ed1',
                  opacity: 0.8,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                  }}
                >
                  <span
                    style={{
                      fontSize: 10,
                      color: '#722ed1',
                      fontWeight: 600,
                    }}
                  >
                    {'\u{1F9E0}'} 思考中… <LoadingOutlined style={{ fontSize: 9 }} />
                  </span>
                  <span
                    style={{
                      fontSize: 10,
                      color: '#d3adf7',
                      fontVariantNumeric: 'tabular-nums',
                    }}
                  >
                    {streamStartMsRef.current
                      ? fmtElapsed(Math.max(0, nowMs - streamStartMsRef.current))
                      : fmtElapsed(Math.max(0, nowMs - startMs))}
                  </span>
                </div>
                <div
                  style={{
                    fontSize: 11,
                    color: '#595959',
                    lineHeight: '17px',
                    whiteSpace: 'pre-wrap',
                    marginTop: 2,
                  }}
                >
                  {stripCodeBlocks(streamingText).displayText}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Scroll-to-bottom floating button */}
        {!isAtBottom && (
          <div
            onClick={scrollToBottom}
            style={{
              position: 'absolute',
              bottom: 8,
              left: '50%',
              transform: 'translateX(-50%)',
              background: '#1890ff',
              color: '#fff',
              borderRadius: 12,
              padding: '2px 12px',
              fontSize: 11,
              cursor: 'pointer',
              boxShadow: '0 2px 8px rgba(0,0,0,.15)',
              zIndex: 10,
            }}
          >
            {'↓'} 最新{unreadCount > 0 && ` (${unreadCount})`}
          </div>
        )}
      </div>
    </div>
  )
}
