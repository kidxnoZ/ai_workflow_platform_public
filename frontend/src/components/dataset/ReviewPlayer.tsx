/**
 * frontend/src/components/dataset/ReviewPlayer.tsx
 *
 * Human review player: auto-advances through review items.
 * Features: editable ASR text field, flag checkboxes (音质差/副语言多/硬编码有误),
 * Space key shortcut to save and advance, progress counter.
 * Submits batches via submitAction({action:'submit_review'}).
 */
import React, { useState, useEffect, useRef, useCallback } from 'react'
import { Button, Checkbox, Input, Typography, Progress, message, Space } from 'antd'
import { LeftOutlined, RightOutlined, SaveOutlined } from '@ant-design/icons'
import type { ReviewItem } from '../../api/datasetIngest'
import { getReviewItems, submitAction } from '../../api/datasetIngest'

const { TextArea } = Input
const { Text } = Typography

const PAGE_SIZE = 50
const BATCH_SUBMIT_SIZE = 20

// ── Types ─────────────────────────────────────────────────────────────────────

export interface ReviewPlayerProps {
  taskId: string
  onComplete: () => void
}

/** Local user edits for a single review item. */
interface ItemEdit {
  text_edited: string
  quality_poor: number
  paralanguage_heavy: number
  hardcode_error: number
  too_short: number
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function filenameFromPath(p: string): string {
  const seg = p.split('/').filter(Boolean)
  return seg[seg.length - 1] || p
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function ReviewPlayer({ taskId, onComplete }: ReviewPlayerProps): React.ReactElement {
  // ── State ──────────────────────────────────────────────────────────────────

  const [items, setItems] = useState<ReviewItem[]>([])
  const [total, setTotal] = useState(0)
  const [currentIndex, setCurrentIndex] = useState(0)
  const [currentPage, setCurrentPage] = useState(0)
  const [loading, setLoading] = useState(true)
  const [finished, setFinished] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [edits, setEdits] = useState<Record<string, ItemEdit>>({})

  // ── Refs (mirror state for stable event handlers) ─────────────────────────

  const itemsRef = useRef<ReviewItem[]>([])
  const totalRef = useRef(0)
  const currentIndexRef = useRef(0)
  const editsRef = useRef<Record<string, ItemEdit>>({})
  const pendingSubmitRef = useRef<ReviewItem[]>([])
  const isSubmittingRef = useRef(false)
  const finishedRef = useRef(false)
  const advancingRef = useRef(false)
  const onCompleteRef = useRef(onComplete)

  // Keep refs in sync with state every render.
  itemsRef.current = items
  totalRef.current = total
  currentIndexRef.current = currentIndex
  editsRef.current = edits
  isSubmittingRef.current = isSubmitting
  finishedRef.current = finished
  onCompleteRef.current = onComplete

  const audioRef = useRef<HTMLAudioElement>(null)

  // ── Data loading ───────────────────────────────────────────────────────────

  const loadPage = useCallback(
    async (page: number) => {
      try {
        const { items: newItems, total: totalCount } = await getReviewItems(
          taskId,
          page,
          PAGE_SIZE,
        )
        setItems((prev) => [...prev, ...newItems])
        setTotal(totalCount)
        setCurrentPage(page)
      } catch {
        message.error('Failed to load review items')
      } finally {
        setLoading(false)
      }
    },
    [taskId],
  )

  // Load first page on mount.
  useEffect(() => {
    loadPage(1)
  }, [loadPage])

  // Load more pages when nearing end of loaded batch.
  useEffect(() => {
    if (loading || finishedRef.current) return
    if (
      currentIndex >= items.length - 10 &&
      items.length < total &&
      currentPage > 0
    ) {
      loadPage(currentPage + 1)
    }
  }, [currentIndex, items.length, total, loading, currentPage, loadPage])

  // ── Derived values ─────────────────────────────────────────────────────────

  const currentItem = items[currentIndex]

  /** Effective text for display: user edit > server edit > original ASR. */
  const displayText = (() => {
    if (!currentItem) return ''
    const edit = edits[currentItem.key]
    if (edit) return edit.text_edited
    return currentItem.text_edited || currentItem.text
  })()

  /** Effective flag value for a checkbox. */
  function effectiveFlag(
    flag: 'quality_poor' | 'paralanguage_heavy' | 'hardcode_error' | 'too_short',
  ): number {
    if (!currentItem) return 0
    const edit = edits[currentItem.key]
    if (edit) return edit[flag]
    return currentItem[flag]
  }

  // ── Edit helpers ───────────────────────────────────────────────────────────

  /** Apply a partial edit for the current item, initialising the entry lazily. */
  const updateEdit = useCallback(
    (partial: Partial<ItemEdit>) => {
      if (!currentItem) return
      setEdits((prev) => {
        const existing = prev[currentItem.key]
        const base: ItemEdit = existing || {
          text_edited: currentItem.text_edited || currentItem.text,
          quality_poor: currentItem.quality_poor,
          paralanguage_heavy: currentItem.paralanguage_heavy,
          hardcode_error: currentItem.hardcode_error,
          too_short: currentItem.too_short,
        }
        return {
          ...prev,
          [currentItem.key]: { ...base, ...partial },
        }
      })
    },
    [currentItem],
  )

  // ── Flush pending submits ──────────────────────────────────────────────────

  const flushPending = useCallback(async () => {
    if (pendingSubmitRef.current.length === 0) return
    const batch = [...pendingSubmitRef.current]
    pendingSubmitRef.current = []
    try {
      await submitAction(taskId, {
        action: 'submit_review',
        step: 'review_session',
        review_items: batch,
      })
    } catch {
      message.error('Failed to submit review items')
      // Restore failed items to the front of the queue.
      pendingSubmitRef.current = [...batch, ...pendingSubmitRef.current]
    }
  }, [taskId])

  // ── Save & advance (stable callback via refs) ──────────────────────────────

  const handleSaveAndAdvance = useCallback(async () => {
    const idx = currentIndexRef.current
    const allItems = itemsRef.current
    const totalCount = totalRef.current
    const item = allItems[idx]

    if (!item) return

    // Save current item if it has edits that differ from the original.
    const edit = editsRef.current[item.key]
    if (edit) {
      const originalText = item.text_edited || item.text
      const isEdited =
        edit.text_edited !== originalText ||
        edit.quality_poor !== item.quality_poor ||
        edit.paralanguage_heavy !== item.paralanguage_heavy ||
        edit.hardcode_error !== item.hardcode_error ||
        edit.too_short !== item.too_short

      if (isEdited) {
        const itemToSubmit: ReviewItem = {
          ...item,
          text_edited: edit.text_edited,
          quality_poor: edit.quality_poor,
          paralanguage_heavy: edit.paralanguage_heavy,
          hardcode_error: edit.hardcode_error,
          too_short: edit.too_short,
        }
        pendingSubmitRef.current.push(itemToSubmit)

        // Clear the edit for this item from state.
        setEdits((prev) => {
          const next = { ...prev }
          delete next[item.key]
          return next
        })
      }
    }

    // Flush if the queue has grown large enough.
    if (pendingSubmitRef.current.length >= BATCH_SUBMIT_SIZE) {
      setIsSubmitting(true)
      await flushPending()
      setIsSubmitting(false)
    }

    // Advance or complete.
    if (idx + 1 >= totalCount) {
      // Final item: flush remaining and finish.
      setIsSubmitting(true)
      await flushPending()
      setFinished(true)
      setIsSubmitting(false)
      onCompleteRef.current()
    } else {
      setCurrentIndex(idx + 1)
    }
  }, [flushPending])

  // ── Auto-play when item changes ────────────────────────────────────────────

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !currentItem) return
    audio.load()
    audio.play().catch(() => {
      // Browser may block autoplay; user can manually press play.
    })
  }, [currentIndex]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Keyboard shortcut: Space ───────────────────────────────────────────────

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      // Only respond to Space when not typing in a text area.
      if (
        e.key === ' ' &&
        !advancingRef.current &&
        document.activeElement?.tagName !== 'TEXTAREA' &&
        document.activeElement?.tagName !== 'INPUT'
      ) {
        e.preventDefault()
        advancingRef.current = true
        handleSaveAndAdvance().finally(() => {
          advancingRef.current = false
        })
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [handleSaveAndAdvance])

  // ── Audio onEnded handler ──────────────────────────────────────────────────

  const handleAudioEnded = useCallback(() => {
    if (advancingRef.current) return
    advancingRef.current = true
    handleSaveAndAdvance().finally(() => {
      advancingRef.current = false
    })
  }, [handleSaveAndAdvance])

  // ── Button handlers ────────────────────────────────────────────────────────

  const handlePrevious = useCallback(() => {
    if (currentIndex <= 0) return
    // Save current item before navigating.
    setEdits((prev) => {
      const item = items[currentIndex]
      if (!item) return prev
      const edit = prev[item.key]
      if (!edit) return prev
      const originalText = item.text_edited || item.text
      const isEdited =
        edit.text_edited !== originalText ||
        edit.quality_poor !== item.quality_poor ||
        edit.paralanguage_heavy !== item.paralanguage_heavy ||
        edit.hardcode_error !== item.hardcode_error
      if (isEdited) {
        const itemToSubmit: ReviewItem = {
          ...item,
          text_edited: edit.text_edited,
          quality_poor: edit.quality_poor,
          paralanguage_heavy: edit.paralanguage_heavy,
          hardcode_error: edit.hardcode_error,
        }
        pendingSubmitRef.current.push(itemToSubmit)
        const next = { ...prev }
        delete next[item.key]
        return next
      }
      return prev
    })
    setCurrentIndex((i) => i - 1)
  }, [currentIndex, items])

  const handleNext = useCallback(() => {
    if (advancingRef.current) return
    advancingRef.current = true
    handleSaveAndAdvance().finally(() => {
      advancingRef.current = false
    })
  }, [handleSaveAndAdvance])

  const handleSaveButtonClick = useCallback(() => {
    if (advancingRef.current) return
    advancingRef.current = true
    handleSaveAndAdvance().finally(() => {
      advancingRef.current = false
    })
  }, [handleSaveAndAdvance])

  // ── Text / flag change handlers ────────────────────────────────────────────

  const handleTextChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      updateEdit({ text_edited: e.target.value })
    },
    [updateEdit],
  )

  const handleFlagChange = useCallback(
    (
      flag: 'quality_poor' | 'paralanguage_heavy' | 'hardcode_error' | 'too_short',
      checked: boolean,
    ) => {
      updateEdit({ [flag]: checked ? 1 : 0 })
    },
    [updateEdit],
  )

  // ── Render states ──────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Text type="secondary">Loading review items...</Text>
      </div>
    )
  }

  if (finished) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Text type="success" strong style={{ fontSize: 16 }}>
          Review complete! All items submitted.
        </Text>
      </div>
    )
  }

  if (!currentItem) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Text type="secondary">No items to review.</Text>
      </div>
    )
  }

  // ── Main render ────────────────────────────────────────────────────────────

  const progressPercent = total > 0 ? Math.round(((currentIndex + 1) / total) * 100) : 0

  return (
    <div style={{ padding: 24, maxWidth: 640, margin: '0 auto' }}>
      {/* ── Progress bar ────────────────────────────────────────────────── */}
      <div style={{ marginBottom: 16 }}>
        <Progress
          percent={progressPercent}
          format={() => `${currentIndex + 1} / ${total}`}
          strokeColor="#1677ff"
          size="small"
        />
      </div>

      {/* ── Navigation row ──────────────────────────────────────────────── */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 20,
        }}
      >
        <Button
          icon={<LeftOutlined />}
          onClick={handlePrevious}
          disabled={currentIndex === 0}
        >
          上一条
        </Button>

        <Text strong style={{ fontSize: 14 }}>
          {filenameFromPath(currentItem.path)}
        </Text>

        <Button
          icon={<RightOutlined />}
          onClick={handleNext}
          disabled={currentIndex + 1 >= total}
        >
          下一条
        </Button>
      </div>

      {/* ── Audio player ────────────────────────────────────────────────── */}
      <div style={{ marginBottom: 20 }}>
        <audio
          ref={audioRef}
          src={currentItem.audio_url}
          controls
          autoPlay
          onEnded={handleAudioEnded}
          style={{ width: '100%' }}
        />
        <Text
          type="secondary"
          style={{ display: 'block', textAlign: 'center', marginTop: 4 }}
        >
          {currentItem.duration != null
            ? `${currentItem.duration.toFixed(1)}s`
            : ''}
        </Text>
      </div>

      {/* ── ASR text (editable) ─────────────────────────────────────────── */}
      <div style={{ marginBottom: 20 }}>
        <Text strong>ASR 文本（可编辑）</Text>
        <TextArea
          value={displayText}
          onChange={handleTextChange}
          rows={3}
          style={{ marginTop: 8 }}
        />
      </div>

      {/* ── Flags (multi-select checkboxes) ──────────────────────────────── */}
      <div style={{ marginBottom: 24 }}>
        <Text strong>标注</Text>
        <div style={{ marginTop: 8 }}>
          <Space wrap>
            <Checkbox
              checked={effectiveFlag('quality_poor') === 1}
              onChange={(e) => handleFlagChange('quality_poor', e.target.checked)}
            >
              音质差
            </Checkbox>
            <Checkbox
              checked={effectiveFlag('paralanguage_heavy') === 1}
              onChange={(e) =>
                handleFlagChange('paralanguage_heavy', e.target.checked)
              }
            >
              副语言多
            </Checkbox>
            <Checkbox
              checked={effectiveFlag('hardcode_error') === 1}
              onChange={(e) =>
                handleFlagChange('hardcode_error', e.target.checked)
              }
            >
              硬编码有误
            </Checkbox>
            <Checkbox
              checked={effectiveFlag('too_short') === 1}
              onChange={(e) => handleFlagChange('too_short', e.target.checked)}
              style={{ color: effectiveFlag('too_short') === 1 ? '#fa8c16' : undefined }}
            >
              过短（自动标注，可取消）
            </Checkbox>
          </Space>
        </div>
      </div>

      {/* ── Save & next button ──────────────────────────────────────────── */}
      <div style={{ textAlign: 'center' }}>
        <Button
          type="primary"
          size="large"
          icon={<SaveOutlined />}
          onClick={handleSaveButtonClick}
          loading={isSubmitting}
        >
          保存并下一条 (Space)
        </Button>
      </div>
    </div>
  )
}
