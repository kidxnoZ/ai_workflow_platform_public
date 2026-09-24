import { Tag } from 'antd'

const COLOR: Record<string, string> = {
  pending: 'default',
  running: 'processing',
  success: 'success',
  failed: 'error',
  awaiting_review: 'warning',
}

const LABEL: Record<string, string> = {
  pending: '排队中',
  running: '运行中',
  success: '成功',
  failed: '失败',
  awaiting_review: '待人工操作',
}

export default function TaskStatusTag({ status }: { status: string }) {
  return <Tag color={COLOR[status] ?? 'default'}>{LABEL[status] ?? status}</Tag>
}
