import { BrowserRouter, Routes, Route, NavLink, Navigate } from 'react-router-dom'
import { Layout, Menu } from 'antd'
import {
  DashboardOutlined,
  SoundOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  AudioOutlined,
  ExperimentOutlined,
  CloudDownloadOutlined,
  BarChartOutlined,
  AuditOutlined,
} from '@ant-design/icons'
import Dashboard from './pages/Dashboard'
import TTSTask from './pages/TTSTask'
import TaskHistory from './pages/TaskHistory'
import BenchmarkResult from './pages/BenchmarkResult'
import TaskDetail from './pages/TaskDetail'
import VoiceList from './pages/VoiceList'
import PromptLab from './pages/PromptLab'
import PromptLabV3 from './pages/PromptLabV3'
import DataProcess from './pages/DataProcess'
import ModelManager from './pages/ModelManager'
import EvaluateCenter from './pages/EvaluateCenter'
import OfflineEvalHub from './pages/OfflineEvalHub'
import OfflineEvalSession from './pages/OfflineEvalSession'
import OfflineEvalResults from './pages/OfflineEvalResults'
import { useLocation } from 'react-router-dom'

const { Sider, Content } = Layout

const NAV_ITEMS = [
  { key: '/', icon: <DashboardOutlined />, label: 'Dashboard' },
  { key: '/tts', icon: <SoundOutlined />, label: '推理任务' },
  { key: '/dataset', icon: <DatabaseOutlined />, label: '数据处理' },
  { key: '/history', icon: <HistoryOutlined />, label: '历史任务' },
  { key: '/voices', icon: <AudioOutlined />, label: '音色列表' },
  { key: '/prompt-lab', icon: <ExperimentOutlined />, label: '克隆实验室' },
  { key: '/model-manager', icon: <CloudDownloadOutlined />, label: '部署模型' },
  { key: '/evaluate', icon: <BarChartOutlined />, label: '评测中心' },
  { key: '/offline-eval', icon: <AuditOutlined />, label: '离线评测' },
]

function AppLayout() {
  const location = useLocation()
  const selectedKey = NAV_ITEMS.find(
    (item) => item.key !== '/' && location.pathname.startsWith(item.key),
  )?.key ?? '/'

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="dark" width={180}>
        <div style={{ color: '#fff', padding: '16px 12px', fontWeight: 600, fontSize: 14 }}>
          AI Workflow
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={NAV_ITEMS.map((item) => ({
            key: item.key,
            icon: item.icon,
            label: <NavLink to={item.key}>{item.label}</NavLink>,
          }))}
        />
      </Sider>
      <Layout>
        <Content style={{ margin: 24 }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/tts" element={<TTSTask />} />
            <Route path="/benchmark/:runId" element={<BenchmarkResult />} />
            <Route path="/task/:taskId" element={<TaskDetail />} />
            <Route path="/dataset" element={<DataProcess />} />
            <Route path="/history" element={<TaskHistory />} />
            <Route path="/voices" element={<VoiceList />} />
            <Route path="/prompt-lab" element={<PromptLabV3 mode="list" />} />
            <Route path="/prompt-lab/new" element={<PromptLabV3 mode="create" />} />
            <Route path="/prompt-lab/:taskId" element={<PromptLabV3 mode="run" />} />
            <Route path="/prompt-lab-v2" element={<PromptLab />} />
            <Route path="/model-manager" element={<ModelManager />} />
            <Route path="/evaluate" element={<EvaluateCenter />} />
            <Route path="/offline-eval" element={<OfflineEvalHub />} />
            <Route path="/offline-eval/:taskId/evaluate" element={<OfflineEvalSession />} />
            <Route path="/offline-eval/:taskId/results" element={<OfflineEvalResults />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AppLayout />
    </BrowserRouter>
  )
}
