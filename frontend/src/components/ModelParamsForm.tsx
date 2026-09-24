import { Form, InputNumber, Select, Slider, Row, Col, Checkbox } from 'antd'
import { ParamSchema } from '../api/client'

interface Props {
  schema: Record<string, ParamSchema>
  values: Record<string, unknown>
  onChange: (key: string, value: unknown) => void
}

export default function ModelParamsForm({ schema, values, onChange }: Props) {
  const entries = Object.entries(schema)
  if (entries.length === 0) {
    return <span style={{ color: '#aaa', fontSize: 13 }}>此模型无可调参数</span>
  }

  return (
    <Row gutter={[16, 8]}>
      {entries.map(([key, s]) => (
        <Col key={key} xs={24} sm={12} md={8}>
          <Form.Item label={s['ui:widget'] === 'checkbox' ? null : s.title} style={{ marginBottom: 0 }}>
            {s['ui:widget'] === 'slider' ? (
              <Row gutter={8} align="middle" wrap={false}>
                <Col flex="auto">
                  <Slider
                    min={s.minimum ?? 0}
                    max={s.maximum ?? 2}
                    step={s.type === 'integer' ? 1 : 0.05}
                    value={(values[key] as number) ?? (s.default as number)}
                    onChange={(v) => onChange(key, v)}
                    tooltip={{ formatter: (v) => String(v) }}
                  />
                </Col>
                <Col style={{ width: 60 }}>
                  <InputNumber
                    min={s.minimum}
                    max={s.maximum}
                    step={s.type === 'integer' ? 1 : 0.05}
                    value={(values[key] as number) ?? (s.default as number)}
                    onChange={(v) => onChange(key, v)}
                    size="small"
                    style={{ width: '100%' }}
                  />
                </Col>
              </Row>
            ) : s['ui:widget'] === 'select' ? (
              <Select
                value={(values[key] as string) ?? (s.default as string)}
                onChange={(v) => onChange(key, v)}
                options={(s.enum ?? []).map((v) => ({ value: v, label: v }))}
                style={{ width: '100%' }}
              />
            ) : s['ui:widget'] === 'checkbox' || s.type === 'boolean' ? (
              <Checkbox
                checked={(values[key] as boolean) ?? (s.default as boolean)}
                onChange={(e) => onChange(key, e.target.checked)}
              >
                {s.title}
              </Checkbox>
            ) : (
              <InputNumber
                min={s.minimum}
                max={s.maximum}
                step={s.type === 'integer' ? 1 : 0.1}
                value={(values[key] as number) ?? (s.default as number)}
                onChange={(v) => onChange(key, v)}
                style={{ width: 120 }}
              />
            )}
          </Form.Item>
        </Col>
      ))}
    </Row>
  )
}
