import { Result } from 'antd'
import { BarChartOutlined } from '@ant-design/icons'

export default function EvaluateCenter() {
  return (
    <Result
      icon={<BarChartOutlined />}
      title="评测中心"
      subTitle="阶段 3 开发中：客观指标 + LLM 文本判断 + 人工结构化反馈，积累模型能力知识库。"
    />
  )
}
