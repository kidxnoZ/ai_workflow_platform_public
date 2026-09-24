import { Select, Space, Tag, Tooltip, Typography } from 'antd'

export interface ContextKey5D {
  character_type: string
  emotion_register: string
  content_type: string
  language_style: string
  special_req: string
}

const DIMENSIONS: Array<{
  field: keyof ContextKey5D
  label: string
  options: string[]
}> = [
  {
    field: 'character_type',
    label: '角色类型',
    options: ['少女', '少年', '青年女', '青年男', '中年女', '中年男', '老年', '特殊（机器/怪物）'],
  },
  {
    field: 'emotion_register',
    label: '情绪基调',
    options: ['中性', '活泼开朗', '沉稳内敛', '情感丰富', '低沉压抑', '夸张戏剧'],
  },
  {
    field: 'content_type',
    label: '内容场景',
    options: ['游戏对话', '过场旁白', '技能台词', '广告宣传', '有声书故事'],
  },
  {
    field: 'language_style',
    label: '语言风格',
    options: ['口语日常', '正式书面', '文言古风', '混合'],
  },
  {
    field: 'special_req',
    label: '特殊要求',
    options: ['无', '带口音', '方言', '情绪极端变化', '低龄儿童感'],
  },
]

export function buildContextKey(tags: ContextKey5D): string {
  return `${tags.character_type}_${tags.emotion_register}_${tags.content_type}_${tags.language_style}_${tags.special_req}`
}

const DEFAULT_TAGS: ContextKey5D = {
  character_type: '少女',
  emotion_register: '中性',
  content_type: '游戏对话',
  language_style: '口语日常',
  special_req: '无',
}

interface Props {
  value?: Partial<ContextKey5D>
  onChange?: (key: string, tags: ContextKey5D) => void
  mode?: 'edit' | 'display'
  disabled?: boolean
}

export default function BusinessContextTag({
  value,
  onChange,
  mode = 'edit',
  disabled = false,
}: Props) {
  const tags: ContextKey5D = { ...DEFAULT_TAGS, ...value }
  const key = buildContextKey(tags)

  if (mode === 'display') {
    return (
      <Tooltip title={key}>
        <Space wrap size={4}>
          {DIMENSIONS.map((d) => (
            <Tag key={d.field} color="blue" style={{ marginRight: 0 }}>
              {tags[d.field]}
            </Tag>
          ))}
        </Space>
      </Tooltip>
    )
  }

  function handleChange(field: keyof ContextKey5D, v: string) {
    const next: ContextKey5D = { ...tags, [field]: v }
    onChange?.(buildContextKey(next), next)
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={8}>
      {DIMENSIONS.map((d) => (
        <Space key={d.field} size={8} style={{ width: '100%' }}>
          <span style={{ minWidth: 64, color: '#666', fontSize: 13 }}>{d.label}</span>
          <Select
            size="small"
            disabled={disabled}
            value={tags[d.field]}
            onChange={(v) => handleChange(d.field, v)}
            options={d.options.map((o) => ({ label: o, value: o }))}
            style={{ minWidth: 160 }}
          />
        </Space>
      ))}
      <Typography.Text type="secondary" copyable style={{ fontSize: 12 }}>
        {key}
      </Typography.Text>
    </Space>
  )
}
