/**
 * frontend/src/components/dataset/DatasetBrowser.tsx
 *
 * Lazy-loading file browser for the "dataset_preview" pause step.
 * Users browse the directory structure and listen to audio files
 * before confirming or skipping to classification.
 *
 * Uses antd Tree with loadData for lazy loading — only loads one
 * directory level per API call, works for datasets of any size (20-30GB+).
 */

import React, { useState, useEffect, useCallback, useRef, useReducer } from 'react'
import { Button, Input, Spin, Tree, Typography } from 'antd'
import type { DataNode, EventDataNode } from 'antd/es/tree'
import { browseDirectory } from '../../api/datasetIngest'
import type { BrowseResult } from '../../api/datasetIngest'

const { TextArea } = Input
const { Text, Title } = Typography

// ── Helpers ─────────────────────────────────────────────────────────────────────

/** Format bytes into a human-readable string. */
function formatBytes(bytes?: number): string {
  if (bytes == null || bytes === 0) return '0B'
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)}GB`
}

// ── Types ───────────────────────────────────────────────────────────────────────

export interface DatasetBrowserProps {
  taskId: string
  structureType: string // "cv_hierarchy" | "flat"
  totalFiles: number
  onConfirm: (description: string) => void
  onSkip: () => void
}

/** Extra metadata stored on each tree node for rendering. */
interface NodeExtra {
  nodeType: 'dir' | 'file'
  name: string
  path: string
  audioCount?: number
  sizeBytes?: number
  isAudio?: boolean
}

// ── Tree helpers ────────────────────────────────────────────────────────────────

/** Build antd DataNode array from a BrowseResult payload. */
function buildTreeNodes(result: BrowseResult): DataNode[] {
  const dirs: DataNode[] = result.dirs.map((d) => ({
    title: d.name,
    key: d.path,
    isLeaf: false,
    extra: {
      nodeType: 'dir' as const,
      name: d.name,
      path: d.path,
      audioCount: d.audio_count,
    },
  }))

  const files: DataNode[] = result.files.map((f) => ({
    title: f.name,
    key: f.path,
    isLeaf: true,
    extra: {
      nodeType: 'file' as const,
      name: f.name,
      path: f.path,
      sizeBytes: f.size_bytes,
      isAudio: f.is_audio,
    },
  }))

  return [...dirs, ...files]
}

/** Immutably update a tree node's children by key. */
function updateTreeChildren(
  nodes: DataNode[],
  key: string,
  children: DataNode[],
): DataNode[] {
  return nodes.map((node) => {
    if (node.key === key) {
      return { ...node, children }
    }
    if (node.children) {
      return { ...node, children: updateTreeChildren(node.children, key, children) }
    }
    return node
  })
}

/** Read the NodeExtra from a tree node. */
function getExtra(node: DataNode): NodeExtra | undefined {
  return (node as any).extra as NodeExtra | undefined
}

// ── Component ───────────────────────────────────────────────────────────────────

export default function DatasetBrowser({
  taskId,
  structureType,
  totalFiles,
  onConfirm,
  onSkip,
}: DatasetBrowserProps): React.ReactElement {
  const [treeData, setTreeData] = useState<DataNode[]>([])
  const [loading, setLoading] = useState(true)
  // Use ref for playingPath so titleRender never needs to change reference.
  // useReducer provides a stable re-render trigger without recreating titleRender.
  const playingPathRef = useRef<string | null>(null)
  const [, forceRender] = useReducer((x: number) => x + 1, 0)
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // ── Load root level on mount ──────────────────────────────────────────────

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    browseDirectory(taskId, '')
      .then((result) => {
        if (cancelled) return
        setTreeData(buildTreeNodes(result))
      })
      .catch(() => {
        if (cancelled) return
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [taskId])

  // ── Lazy-load children when a directory is expanded ───────────────────────

  const onLoadData = useCallback(
    (node: EventDataNode<DataNode>): Promise<void> => {
      return new Promise<void>(async (resolve, reject) => {
        const extra = getExtra(node)
        if (!extra || extra.nodeType !== 'dir') {
          resolve()
          return
        }
        try {
          const result = await browseDirectory(taskId, extra.path)
          const children = buildTreeNodes(result)
          setTreeData((prev) =>
            updateTreeChildren(prev, node.key as string, children),
          )
          resolve()
        } catch {
          reject()
        }
      })
    },
    [taskId],
  )

  // ── Title renderer — stable ref, never changes, reads playingPath from ref ──

  const titleRender = useCallback(
    (node: DataNode): React.ReactNode => {
      const e = getExtra(node)
      if (!e) return node.title as React.ReactNode

      if (e.nodeType === 'dir') {
        return (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <span style={{ fontSize: 14 }}>📁</span>
            <span>{e.name}</span>
            {e.audioCount != null && (
              <span style={{ color: '#8c8c8c', fontSize: 11, marginLeft: 4 }}>
                ({e.audioCount})
              </span>
            )}
          </span>
        )
      }

      // File node — read from ref (stable, no recreate on play change)
      const isPlaying = playingPathRef.current === e.path
      return (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontSize: 13 }}>{e.isAudio ? '🔊' : '📄'}</span>
          <span>{e.name}</span>
          {e.sizeBytes != null && (
            <span style={{ color: '#8c8c8c', fontSize: 11 }}>
              {formatBytes(e.sizeBytes)}
            </span>
          )}
          {e.isAudio && (
            <Button
              size="small"
              type="link"
              onClick={(ev) => {
                ev.stopPropagation()
                playingPathRef.current = playingPathRef.current === e.path ? null : e.path
                forceRender()  // trigger re-render without recreating titleRender
              }}
              style={{ padding: '0 2px', fontSize: 11, height: 20, lineHeight: '20px' }}
              title={isPlaying ? '停止播放' : '试听'}
            >
              {isPlaying ? '⏹' : '▶'}
            </Button>
          )}
        </span>
      )
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],  // no deps — reads playingPath from ref, forceRender triggers re-render
  )

  // ── Button handlers ───────────────────────────────────────────────────────

  const handleConfirm = async () => {
    setSubmitting(true)
    try {
      await onConfirm(description.trim())
    } catch {
      // parent handles the error toast
    } finally {
      setSubmitting(false)
    }
  }

  const handleSkip = async () => {
    setSubmitting(true)
    try {
      await onSkip()
    } catch {
      // parent handles the error toast
    } finally {
      setSubmitting(false)
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  const structureLabel =
    structureType === 'cv_hierarchy' ? 'CV 层级结构' : '扁平结构'

  return (
    <div
      style={{
        background: '#fff',
        borderRadius: 8,
        border: '1px solid #d9d9d9',
        padding: 16,
      }}
    >
      {/* Header */}
      <div style={{ marginBottom: 12 }}>
        <Title level={5} style={{ margin: 0 }}>
          数据集浏览
        </Title>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {structureLabel}，{totalFiles} 个音频文件
        </Text>
      </div>

      {/* Tree */}
      <div
        style={{
          border: '1px solid #f0f0f0',
          borderRadius: 4,
          padding: '8px 12px',
          maxHeight: 360,
          overflow: 'auto',
          background: '#fafafa',
        }}
      >
        {loading ? (
          <div style={{ textAlign: 'center', padding: 32 }}>
            <Spin size="small" />
            <div style={{ marginTop: 8, fontSize: 12, color: '#8c8c8c' }}>
              加载目录结构...
            </div>
          </div>
        ) : treeData.length === 0 ? (
          <div
            style={{
              textAlign: 'center',
              padding: 32,
              color: '#8c8c8c',
              fontSize: 13,
            }}
          >
            目录为空
          </div>
        ) : (
          <Tree
            showLine={{ showLeafIcon: false }}
            loadData={onLoadData}
            treeData={treeData}
            titleRender={titleRender}
          />
        )}
      </div>

      {/* Inline audio player */}
      {playingPathRef.current && (
        <div
          style={{
            marginTop: 12,
            padding: '8px 12px',
            background: '#f5f5f5',
            borderRadius: 4,
          }}
        >
          <div
            style={{ marginBottom: 4, fontSize: 12, color: '#595959' }}
          >
            正在播放：{(playingPathRef.current ?? '').split('/').pop() || playingPathRef.current}
          </div>
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <audio
            controls
            autoPlay
            src={`/api/dataset-ingest/${taskId}/output-audio?path=${encodeURIComponent(playingPathRef.current ?? '')}`}
            style={{ width: '100%', height: 32 }}
          />
        </div>
      )}

      {/* Description textarea */}
      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 6, fontSize: 13, fontWeight: 500 }}>
          根据浏览结果，描述你的理解：
        </div>
        <TextArea
          rows={3}
          placeholder="请描述数据集结构，例如：每个 CV 文件夹下有多个角色，文件命名包含角色名和场景编号，少数文件夹是多人对话…"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          disabled={submitting}
        />
      </div>

      {/* Action buttons */}
      <div
        style={{
          marginTop: 16,
          display: 'flex',
          justifyContent: 'flex-end',
          gap: 8,
        }}
      >
        <Button onClick={handleSkip} loading={submitting}>
          跳过，直接分类
        </Button>
        <Button type="primary" onClick={handleConfirm} loading={submitting}>
          确认，开始分类
        </Button>
      </div>
    </div>
  )
}
