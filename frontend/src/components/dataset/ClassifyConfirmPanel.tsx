/**
 * frontend/src/components/dataset/ClassifyConfirmPanel.tsx
 *
 * Shown when SSE 'pause' event arrives for step 'classify_review'.
 * Displays: collapsible generated script preview, directory tree,
 * LLM-proposed MERGE_TASKS list (confirm/reject each), feedback input,
 * and Approve / Request Correction buttons.
 */
import React, { useState } from 'react'
import {
  Alert, Button, Checkbox, Collapse, Input, Space, Table, Tag, Tree, Typography,
} from 'antd'
import { CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons'

const { TextArea } = Input
const { Text, Title } = Typography

export interface MergeCandidate {
  src_speaker: string
  dst_speaker: string
  reason: string
}

export interface ClassifyConfirmPanelProps {
  taskId: string
  dirTree: unknown
  scriptPreview: string
  mergeCandidates: MergeCandidate[]
  integrityOk: boolean
  integrityDetail: string
  onAction: (action: 'approve' | 'reject', opts?: {
    feedback?: string
    confirmedMerges?: MergeCandidate[]
    saveExperience?: boolean
  }) => void
}

function treeToAntd(node: unknown): object[] {
  if (!node || typeof node !== 'object') return []
  const n = node as { name: string; type: string; file_count?: number; children?: unknown[] }
  return [{
    title: n.type === 'dir'
      ? `${n.name}/  (${n.file_count ?? 0} 文件)`
      : n.name,
    key: n.name,
    children: n.children ? n.children.flatMap(treeToAntd) : undefined,
  }]
}

export default function ClassifyConfirmPanel({
  dirTree,
  scriptPreview,
  mergeCandidates,
  integrityOk,
  integrityDetail,
  onAction,
}: ClassifyConfirmPanelProps): React.ReactElement {
  const [feedback, setFeedback] = useState('')
  const [rejecting, setRejecting] = useState(false)
  const [saveExperience, setSaveExperience] = useState(false)
  const [checkedMerges, setCheckedMerges] = useState<Set<number>>(
    new Set(mergeCandidates.map((_, i) => i))
  )

  const toggleMerge = (idx: number) => {
    setCheckedMerges(prev => {
      const next = new Set(prev)
      next.has(idx) ? next.delete(idx) : next.add(idx)
      return next
    })
  }

  const handleApprove = () => {
    const confirmedMerges = mergeCandidates.filter((_, i) => checkedMerges.has(i))
    onAction('approve', { confirmedMerges, saveExperience })
  }

  const handleReject = () => {
    if (!feedback.trim()) return
    onAction('reject', { feedback: feedback.trim() })
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="large">
      {/* Integrity status */}
      <Alert
        type={integrityOk ? 'success' : 'error'}
        icon={integrityOk ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
        message={integrityOk ? '完整性校验通过' : '完整性校验失败'}
        description={integrityDetail}
        showIcon
      />

      {/* Script preview */}
      <Collapse
        items={[{
          key: 'script',
          label: '生成的分类脚本（点击展开）',
          children: (
            <pre style={{ maxHeight: 300, overflow: 'auto', fontSize: 12, background: '#1e1e1e', color: '#d4d4d4', padding: 12, borderRadius: 4 }}>
              {scriptPreview}
            </pre>
          ),
        }]}
      />

      {/* Directory tree */}
      <div>
        <Title level={5} style={{ marginBottom: 8 }}>目录树预览</Title>
        <div style={{ maxHeight: 300, overflow: 'auto', border: '1px solid #d9d9d9', borderRadius: 6, padding: 8 }}>
          <Tree
            treeData={treeToAntd(dirTree) as any}
            defaultExpandAll
            showLine
          />
        </div>
      </div>

      {/* Merge candidates */}
      {mergeCandidates.length > 0 && (
        <div>
          <Title level={5} style={{ marginBottom: 8 }}>
            合并候选列表（勾选即确认合并）
          </Title>
          <Space direction="vertical" style={{ width: '100%' }}>
            {mergeCandidates.map((m, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Checkbox
                  checked={checkedMerges.has(i)}
                  onChange={() => toggleMerge(i)}
                />
                <Tag color="blue">{m.src_speaker}</Tag>
                <Text>→ 合并进</Text>
                <Tag color="green">{m.dst_speaker}</Tag>
                <Text type="secondary">{m.reason}</Text>
              </div>
            ))}
          </Space>
        </div>
      )}

      {/* Experience gate — unchecked by default, must be explicitly confirmed */}
      <div style={{ padding: '8px 12px', background: '#fffbe6', borderRadius: 6, border: '1px solid #ffe58f' }}>
        <Checkbox
          checked={saveExperience}
          onChange={e => setSaveExperience(e.target.checked)}
        >
          <span style={{ fontSize: 12 }}>
            将此次分类规则写入经验库
            <span style={{ color: '#8c8c8c', marginLeft: 6 }}>
              （仅在确认分类完全正确后勾选，错误经验会影响后续数据集）
            </span>
          </span>
        </Checkbox>
      </div>

      {/* Actions */}
      {!rejecting ? (
        <Space>
          <Button
            type="primary"
            onClick={handleApprove}
            disabled={!integrityOk}
          >
            通过，执行分类
          </Button>
          <Button danger onClick={() => setRejecting(true)}>
            有问题，告诉 Agent
          </Button>
        </Space>
      ) : (
        <Space direction="vertical" style={{ width: '100%' }}>
          <TextArea
            rows={3}
            placeholder="描述问题，例如：actor_023 应该归入 actor_018，因为..."
            value={feedback}
            onChange={e => setFeedback(e.target.value)}
          />
          <Space>
            <Button type="primary" onClick={handleReject} disabled={!feedback.trim()}>
              提交修正
            </Button>
            <Button onClick={() => { setRejecting(false); setFeedback('') }}>
              取消
            </Button>
          </Space>
        </Space>
      )}
    </Space>
  )
}
