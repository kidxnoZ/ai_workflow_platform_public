/**
 * frontend/src/components/dataset/NewIngestForm.tsx
 *
 * Task creation form: data source (server path or zip upload),
 * output path, dataset metadata fields, and step checkboxes.
 * Submits via createIngestTask(); on success navigates to execution view.
 */
import React, { useState } from 'react'
import {
  Form,
  Input,
  Radio,
  Checkbox,
  Select,
  Upload,
  Button,
  message,
  Space,
  Row,
  Col,
} from 'antd'
import { InboxOutlined } from '@ant-design/icons'
import type { UploadFile } from 'antd/es/upload/interface'
import { createIngestTask, type IngestRequest } from '../../api/datasetIngest'
import { api } from '../../api/client'

export interface NewIngestFormProps {
  onTaskCreated: (taskId: string) => void
}

const STEP_OPTIONS = [
  { key: 'classify', label: '数据分类 + metadata 构建' },
  { key: 'asr', label: 'data.jsonl 标注（ASR）' },
  { key: 'silence_trim', label: '首尾静音替换' },
  { key: 'pause_compress', label: '压缩停顿' },
  { key: 'gain', label: '音量增益' },
  { key: 'segment', label: '切句' },
  { key: 'flag_short', label: '过短片段标注' },
  { key: 'flag_richtext', label: '富文本过多标注' },
]

const DOMAIN_OPTIONS = [
  { value: '游戏', label: '游戏' },
  { value: '动漫', label: '动漫' },
  { value: '素人', label: '素人' },
  { value: '有声书', label: '有声书' },
]

const LANGUAGE_OPTIONS = [
  { value: '中文', label: '中文' },
  { value: '英文', label: '英文' },
  { value: '粤语', label: '粤语' },
  { value: '日语', label: '日语' },
]

const MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024 // 2 GB

export default function NewIngestForm({ onTaskCreated }: NewIngestFormProps): React.ReactElement {
  const [form] = Form.useForm()
  const [submitting, setSubmitting] = useState(false)
  const [sourceType, setSourceType] = useState<'server_path' | 'upload'>('server_path')
  const [fileList, setFileList] = useState<UploadFile[]>([])

  const handleSourceTypeChange = (e: { target: { value?: string } }) => {
    setSourceType(e.target.value as 'server_path' | 'upload')
  }

  const handleBeforeUpload = (file: File) => {
    const isZip = file.type === 'application/zip' ||
      file.name.toLowerCase().endsWith('.zip')
    if (!isZip) {
      message.error('只允许上传 .zip 压缩包')
      return Upload.LIST_IGNORE
    }
    if (file.size > MAX_FILE_SIZE) {
      message.error('文件大小不能超过 2GB，请改用服务器路径方式')
      return Upload.LIST_IGNORE
    }
    // Prevent auto-upload; we upload manually on submit
    return false
  }

  const handleUploadChange = (info: { fileList: UploadFile[] }) => {
    setFileList(info.fileList)
  }

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields()

      if (sourceType === 'server_path' && !values.source_path) {
        message.error('请输入服务器路径')
        return
      }
      if (sourceType === 'upload' && fileList.length === 0) {
        message.error('请选择上传文件')
        return
      }

      setSubmitting(true)

      let uploadFileId: string | undefined = undefined

      if (sourceType === 'upload' && fileList.length > 0) {
        const originFile = fileList[0].originFileObj
        if (originFile) {
          const fileResult = await api.uploadFile(originFile as File)
          uploadFileId = fileResult.file_id
        }
      }

      const req: IngestRequest = {
        source_type: sourceType,
        source_path: sourceType === 'server_path' ? values.source_path : undefined,
        upload_file_id: uploadFileId,
        output_path: values.output_path,
        project_code: values.project_code,
        source_label: values.source_label,
        copyright: values.copyright,
        domain: values.domain,
        language: values.language,
        steps: values.steps,
      }

      const result = await createIngestTask(req)
      message.success('任务创建成功')
      onTaskCreated(result.task_id)
    } catch (err) {
      if (err instanceof Error) {
        message.error(err.message || '创建失败')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Form
      form={form}
      layout="vertical"
      initialValues={{
        source_type: 'server_path',
        copyright: '互娱版权',
        domain: '游戏',
        language: '中文',
        steps: STEP_OPTIONS.map((s) => s.key),
      }}
      style={{ maxWidth: 720 }}
    >
      {/* ── 数据来源 ─────────────────────────────────────────────── */}
      <Form.Item
        label="数据来源"
        name="source_type"
        rules={[{ required: true, message: '请选择数据来源' }]}
      >
        <Radio.Group onChange={handleSourceTypeChange}>
          <Radio value="server_path">服务器路径</Radio>
          <Radio value="upload">上传压缩包</Radio>
        </Radio.Group>
      </Form.Item>

      {sourceType === 'server_path' && (
        <Form.Item
          label="服务器路径"
          name="source_path"
          rules={[{ required: true, message: '请输入服务器路径' }]}
        >
          <Input placeholder="/path/to/raw/data" />
        </Form.Item>
      )}

      {sourceType === 'upload' && (
        <Form.Item label="上传压缩包">
          <Upload.Dragger
            accept=".zip"
            maxCount={1}
            fileList={fileList}
            beforeUpload={handleBeforeUpload}
            onChange={handleUploadChange}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
            <p className="ant-upload-hint">仅支持 .zip 格式，最大 2GB。超出请使用服务器路径方式</p>
          </Upload.Dragger>
        </Form.Item>
      )}

      {/* ── 输出路径 ─────────────────────────────────────────────── */}
      <Form.Item
        label="输出路径"
        name="output_path"
        rules={[{ required: true, message: '请输入输出路径' }]}
      >
        <Input placeholder="/project/tts/tts_expdata/macheng/..." />
      </Form.Item>

      {/* ── 元信息 ───────────────────────────────────────────────── */}
      <Row gutter={16}>
        <Col span={12}>
          <Form.Item
            label="项目代号"
            name="project_code"
            rules={[{ required: true, message: '请输入项目代号' }]}
          >
            <Input placeholder="G112" />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item
            label="数据来源描述"
            name="source_label"
          >
            <Input placeholder="G112游戏" />
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col span={8}>
          <Form.Item
            label="版权方"
            name="copyright"
          >
            <Input />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item
            label="领域"
            name="domain"
          >
            <Select options={DOMAIN_OPTIONS} />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item
            label="语种"
            name="language"
          >
            <Select options={LANGUAGE_OPTIONS} />
          </Form.Item>
        </Col>
      </Row>

      {/* ── 执行步骤 ─────────────────────────────────────────────── */}
      <Form.Item
        label="执行步骤（按顺序，全部默认勾选）"
        name="steps"
        rules={[{ required: true, message: '请至少选择一个执行步骤' }]}
      >
        <Checkbox.Group
          options={STEP_OPTIONS.map((s) => ({
            label: s.label,
            value: s.key,
          }))}
        />
      </Form.Item>

      {/* ── 提交按钮 ─────────────────────────────────────────────── */}
      <Form.Item>
        <Space style={{ justifyContent: 'flex-end', width: '100%' }}>
          <Button
            type="primary"
            loading={submitting}
            onClick={handleSubmit}
          >
            开始处理
          </Button>
        </Space>
      </Form.Item>
    </Form>
  )
}
