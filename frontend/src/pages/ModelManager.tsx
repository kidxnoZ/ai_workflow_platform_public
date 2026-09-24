import { Result } from 'antd'
import { CloudDownloadOutlined } from '@ant-design/icons'

export default function ModelManager() {
  return (
    <Result
      icon={<CloudDownloadOutlined />}
      title="部署模型"
      subTitle="阶段 4 开发中：给定模型名称或 URL，自动完成环境安装、runner 生成、接口测试，注册为 staging 后人工审批上线。"
    />
  )
}
