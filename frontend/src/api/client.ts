import axios from 'axios'

const http = axios.create({ baseURL: '' })

export default http

export interface TaskOut {
  id: string
  type: string
  status: 'pending' | 'running' | 'success' | 'failed' | 'awaiting_review'
  created_at: string
  updated_at: string
  finished_at: string | null
  input: unknown
  result: unknown
  logs: { time: string; level: string; msg: string }[]
  error: string | null
}

export interface FileOut {
  file_id: string
  original_name: string
  size_bytes: number | null
}

export interface ParamSchema {
  type: 'number' | 'integer' | 'string' | 'boolean'
  default: number | string | boolean
  minimum?: number
  maximum?: number
  title: string
  'ui:widget': 'slider' | 'input' | 'select' | 'checkbox'
  enum?: string[]
}

export interface ModelInfo {
  id: string
  display_name: string
  status: string
  model_type: string
  params_schema: { properties?: Record<string, ParamSchema> }
}

export interface PromptInfo {
  basename: string
  source_path: string
  exists_on_server: boolean
  item_count: number
}

export interface AnalyzeResult {
  total_items: number
  prompts: PromptInfo[]
  missing_prompts: string[]
  has_any_prompt: boolean
  empty_source_path_count: number
}

export interface BenchmarkRunListItem {
  id: string
  name: string | null
  status: string
  model_ids: string[]
  created_at: string
  finished_at: string | null
  task_count: number
}

export interface BenchmarkTaskSummary {
  task_id: string
  model_id: string
  status: string
  total: number | null
  ok: number | null
}

export interface BenchmarkRunOut {
  id: string
  name: string | null
  status: string
  tasks: BenchmarkTaskSummary[]
}

export interface ElevenLabsVoiceOut {
  voice_id: string
  voice_name: string
  prompt_hash: string
  task_id: string | null
  created_at: string
  deleted_at: string | null
}

export interface MinimaxVoiceOut {
  provider_voice_id: string
  prompt_hash: string
  task_id: string | null
  created_at: string
  deleted_at: string | null
}

export interface DoubaoVoiceOut {
  speaker_id: string
  prompt_hash: string
  task_id: string | null
  created_at: string
}

// ── Agent 1: 克隆实验室 ───────────────────────────────────────────────────────

export interface ContextKey5D {
  character_type: string
  emotion_register: string
  content_type: string
  language_style: string
  special_req: string
}

export interface PlanItem {
  combo_id: string
  label: string
  prompt_wav_paths: string[]
  model_id: string
  params: Record<string, unknown>
  priority: number
  reasoning?: string
}

export interface SynthesisResult {
  text_idx: string
  text: string
  combo_id: string
  model_id: string
  audio_url: string
  sub_task_id: string
}

export interface ReviewBody {
  human_selections: Record<string, string>
  evaluations: Record<string, { score: number; problem_tags: string[]; notes: string }>
}

export interface ExperimentHistoryItem {
  business_context_key: string
  prompt_combo: string
  model_id: string
  avg_score: number | null
  n_samples: number
}

export interface KBRecord {
  prompt_combo: string
  model_id: string
  avg_score: number | null
  n_samples: number
}

// Parsed shape of a prompt_experiment task's input field
export interface PhaseTiming {
  began_at?: string
  ended_at?: string
}

export interface ExperimentInput {
  phase: string
  prompts: { file_id: string; stored_path: string; asr_text: string }[]
  business_context: ContextKey5D
  context_key: string
  model_ids: string[]
  params_map: Record<string, Record<string, unknown>>
  test_text_count: number
  synthesis_scene?: string
  generated_texts?: string[]
  confirmed_texts?: string[]
  split_prompts?: Record<string, string>
  combo_plan?: PlanItem[]
  confirmed_plan?: PlanItem[]
  synthesis_progress?: { done: number; total: number }
  synthesis_results?: SynthesisResult[]
  agent_reasoning?: string
  kb_query_result?: { query_key: string; records: KBRecord[] }
  phase_timings?: Record<string, PhaseTiming>
}

// ── Agent 1 v3: 克隆实验室 (多轮迭代) ─────────────────────────────────────────

export interface V3PromptAnnotation {
  file_id: string
  asr_text: string
  annotation: {
    recording_env: string
    speaking_pace: string
    style: string
    paralanguage: string
    character: string
    scene_suitability?: string[]
    notes?: string
  }
}

export interface V3Combo {
  combo_id: string
  type: 'exploit' | 'diagnostic' | 'explore'
  prompt_ids: string[]
  model_id: string
  text_strategy: string
  text_variants?: Record<string, string>
  params?: Record<string, unknown>
  reasoning?: string
  hypothesis_ref?: string | null
}

export interface V3Hypothesis {
  id: string
  content: string
  status: 'untested' | 'testing' | 'confirmed' | 'refuted' | 'inconclusive'
  source?: string
}

export interface V3TaskPlan {
  plan_summary: string
  round1_goal: string
  explore_rationale?: string
  expected_directions?: { condition: string; action: string }[]
  initial_hypotheses: V3Hypothesis[]
  estimated_rounds: string
  cold_start_notice?: string | null
}

export interface V3RoundPlan {
  round: number
  base_texts: string[]
  combos: V3Combo[]
  hypotheses_this_round?: V3Hypothesis[]
  plan_summary: string
}

export interface V3SynthesisResult {
  combo_id: string
  round: number
  text_idx: number
  text: string
  model_id: string
  prompt_ids: string[]
  audio_url: string | null
  audio_label: string
  sub_task_id: string | null
  status: 'success' | 'failed'
  error_reason: string | null
}

export interface V3EvaluationBody {
  round: number
  rankings_by_text: Record<string, { combo_id: string; rank: number }[]>
  per_combo?: Record<string, { vs_target?: string; issues?: string[]; notes?: string }>
  winner?: string | null
  next_round_direction?: string
  finish_requested?: boolean
}

export interface V3WinnerConfig {
  model_id: string
  prompt_ids: string[]
  prompt_paths: string[]
  text_strategy: string
  params: Record<string, unknown>
  text_variants: Record<string, string>
  scene_type: string
  scene_form: Record<string, unknown>
}

export interface V3ExperimentInput {
  phase: string
  prompts: { file_id: string; stored_path: string; asr_text: string; annotation?: Record<string, unknown> }[]
  scene_type: string
  scene_description: string
  scene_form: Record<string, unknown>
  model_ids: string[]
  params_map: Record<string, Record<string, unknown>>
  user_texts: string[]
  round: number
  task_history: unknown[]
  hypotheses: V3Hypothesis[]
  task_plan?: V3TaskPlan
  current_round_plan?: V3RoundPlan
  confirmed_combo_ids?: string[]
  synthesis_results?: V3SynthesisResult[]
  synthesis_progress?: { done: number; total: number }
  current_synth_model?: string
  current_sub_task_id?: string
  pending_evaluation?: V3EvaluationBody & { winner?: string }
  user_direction?: string
  experience_file_path?: string
  agent_streaming?: string
  agent_steps?: { phase: string; text: string; ts: string }[]
  phase_timings?: Record<string, PhaseTiming>
  // Agent loop (v3.3)
  agent_events?: AgentEvent[]
  portfolio?: PortfolioItem[]
  convergence_assessment?: string
  _review_message?: string
}

// ── Agent Loop types (v3.3) ─────────────────────────────────────────────────

export interface AgentEvent {
  type: 'thinking' | 'tool_call' | 'tool_result' | 'message'
  ts: string
  start_ts?: string
  content?: string
  text?: string
  tool_name?: string
  tool_input?: unknown
  tool_result_summary?: string
  tool_result_data?: unknown
}

export interface PortfolioItem {
  strategy_id: string
  model_id: string
  prompt_ids?: string[]
  text_strategy?: string
  params?: Record<string, unknown>
  profile: string
  best_for?: string[]
  avg_rank?: number
  vs_target_mode?: string
}

export const api = {
  // Files
  uploadFile: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post<FileOut>('/api/files/upload', fd).then((r) => r.data)
  },

  // Models
  getModels: () =>
    http.get<{ models: ModelInfo[] }>('/api/tts/models').then((r) => r.data.models),

  // TTS
  analyzeJsonl: (jsonlFileId: string) =>
    http
      .post<AnalyzeResult>('/api/tts/analyze-jsonl', { jsonl_file_id: jsonlFileId })
      .then((r) => r.data),

  runTTS: (
    jsonlFileId: string,
    modelId: string,
    promptFileId?: string,
    promptMap: Record<string, string> = {},
    params: Record<string, unknown> = {},
  ) =>
    http
      .post<{ task_id: string }>('/api/tts/run', {
        jsonl_file_id: jsonlFileId,
        model_id: modelId,
        prompt_audio_file_id: promptFileId ?? null,
        prompt_map: promptMap,
        params,
      })
      .then((r) => r.data),

  runBenchmark: (body: {
    jsonl_file_id: string
    model_ids: string[]
    prompt_audio_file_id?: string
    prompt_map?: Record<string, string>
    params_per_model?: Record<string, Record<string, unknown>>
    name?: string
  }) =>
    http
      .post<{ benchmark_run_id: string; task_ids: string[]; status: string }>(
        '/api/tts/benchmark',
        body,
      )
      .then((r) => r.data),

  getBenchmark: (runId: string) =>
    http.get<BenchmarkRunOut>(`/api/tts/benchmark/${runId}`).then((r) => r.data),

  listBenchmarks: (params?: { limit?: number; offset?: number }) =>
    http
      .get<{ items: BenchmarkRunListItem[]; total: number }>('/api/tts/benchmarks', { params })
      .then((r) => r.data),

  // Dataset
  processDataset: (files: File[]) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return http.post<{ task_id: string }>('/api/dataset/process', fd).then((r) => r.data)
  },

  // Tasks
  getTask: (taskId: string) => http.get<TaskOut>(`/api/tasks/${taskId}`).then((r) => r.data),
  listTasks: (params?: { type?: string; limit?: number; offset?: number }) =>
    http.get<{ items: TaskOut[]; total: number }>('/api/tasks', { params }).then((r) => r.data),

  cancelTask: (taskId: string) =>
    http.post<{ ok: boolean }>(`/api/agent/tasks/${taskId}/cancel`).then((r) => r.data),

  // Voices
  listVoices: (includeDeleted = false) =>
    http
      .get<{ elevenlabs: ElevenLabsVoiceOut[]; minimax: MinimaxVoiceOut[]; doubao: DoubaoVoiceOut[] }>('/api/voices', {
        params: { include_deleted: includeDeleted },
      })
      .then((r) => r.data),

  deleteVoice: (voiceId: string) =>
    http.delete<{ ok: boolean }>(`/api/voices/elevenlabs/${voiceId}`).then((r) => r.data),

  deleteMinimaxVoice: (providerVoiceId: string) =>
    http.delete<{ ok: boolean }>(`/api/voices/minimax/${providerVoiceId}`).then((r) => r.data),

  // Agent 1: 克隆实验室
  createPromptExperiment: (body: {
    prompt_file_ids: string[]
    prompt_texts?: Record<string, string>
    business_context: ContextKey5D
    model_ids: string[]
    params_map?: Record<string, Record<string, unknown>>
    test_text_count?: number
    synthesis_scene?: string
  }) =>
    http
      .post<{ task_id: string }>('/api/agent/prompt-experiment', body)
      .then((r) => r.data),

  getPromptExperiment: (taskId: string) =>
    http.get<TaskOut>(`/api/agent/prompt-experiment/${taskId}`).then((r) => r.data),

  confirmAsr: (taskId: string, asr_texts: string[]) =>
    http
      .post<{ ok: boolean }>(`/api/agent/prompt-experiment/${taskId}/confirm-asr`, { asr_texts })
      .then((r) => r.data),

  confirmTexts: (taskId: string, confirmed_texts: string[]) =>
    http
      .post<{ ok: boolean }>(`/api/agent/prompt-experiment/${taskId}/confirm-texts`, { confirmed_texts })
      .then((r) => r.data),

  confirmPlan: (taskId: string, confirmed_plan: PlanItem[]) =>
    http
      .post<{ ok: boolean }>(`/api/agent/prompt-experiment/${taskId}/confirm-plan`, { confirmed_plan })
      .then((r) => r.data),

  submitReview: (taskId: string, body: ReviewBody) =>
    http
      .post<{ ok: boolean }>(`/api/agent/prompt-experiment/${taskId}/review`, body)
      .then((r) => r.data),

  getExperimentHistory: (context_key?: string) =>
    http
      .get<{ items: ExperimentHistoryItem[]; total: number }>(
        '/api/agent/prompt-experiment/history',
        { params: context_key ? { context_key } : {} },
      )
      .then((r) => r.data),

  // ── Agent 1 v3: 克隆实验室（多轮迭代） ─────────────────────────────────────────

  createExperimentV3: (body: {
    prompt_file_ids: string[]
    prompt_annotations?: Record<string, Record<string, string>>
    scene_type: string
    scene_description?: string
    scene_form?: Record<string, unknown>
    model_ids: string[]
    params_map?: Record<string, Record<string, unknown>>
    user_texts?: string[]
    user_texts_file_id?: string
    max_synthesis_per_round?: number
    auto_text_count?: number  // 0=不限，>0=Agent自动生成指定条数
  }) =>
    http
      .post<{ task_id: string }>('/api/agent/prompt-experiment-v3', body)
      .then((r) => r.data),

  getExperimentV3: (taskId: string) =>
    http.get<TaskOut>(`/api/agent/prompt-experiment-v3/${taskId}`).then((r) => r.data),

  getExperimentV3History: (scene_type?: string) =>
    http
      .get<{ items: TaskOut[]; total: number }>(
        '/api/agent/prompt-experiment-v3/history',
        { params: scene_type ? { scene_type } : {} },
      )
      .then((r) => r.data),

  confirmAsrV3: (taskId: string, prompts: V3PromptAnnotation[]) =>
    http
      .post<{ ok: boolean }>(`/api/agent/prompt-experiment-v3/${taskId}/confirm-asr`, { prompts })
      .then((r) => r.data),

  confirmPlanV3: (taskId: string, body: { approved?: boolean; user_direction?: string }) =>
    http
      .post<{ ok: boolean }>(`/api/agent/prompt-experiment-v3/${taskId}/confirm-plan`, body)
      .then((r) => r.data),

  confirmRoundV3: (taskId: string, confirmed_combo_ids: string[]) =>
    http
      .post<{ ok: boolean }>(`/api/agent/prompt-experiment-v3/${taskId}/confirm-round`, { confirmed_combo_ids })
      .then((r) => r.data),

  submitEvaluationV3: (taskId: string, body: V3EvaluationBody) =>
    http
      .post<{ ok: boolean }>(`/api/agent/prompt-experiment-v3/${taskId}/submit-evaluation`, body)
      .then((r) => r.data),

  cancelExperimentV3: (taskId: string) =>
    http
      .post<{ ok: boolean }>(`/api/agent/prompt-experiment-v3/${taskId}/cancel`)
      .then((r) => r.data),

  getWinnerConfig: (taskId: string) =>
    http
      .get<V3WinnerConfig>(`/api/agent/prompt-experiment-v3/${taskId}/winner-config`)
      .then((r) => r.data),

  getPortfolio: (taskId: string) =>
    http
      .get<{ portfolio: PortfolioItem[]; summary: string; total_rounds: number; reason: string }>(
        `/api/agent/prompt-experiment-v3/${taskId}/portfolio`,
      )
      .then((r) => r.data),

  getKbCoverage: () =>
    http
      .get<{ content: string }>('/api/agent/prompt-experiment-v3/kb/coverage')
      .then((r) => r.data),

  triggerAggregation: () =>
    http
      .post<{ ok: boolean }>('/api/agent/prompt-experiment-v3/aggregate')
      .then((r) => r.data),
}
