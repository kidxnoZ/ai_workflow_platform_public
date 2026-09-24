/**
 * frontend/src/api/datasetIngest.ts
 *
 * REST + SSE client for the dataset ingest API.
 * SSE connection managed via EventSource; REST calls via axios client.
 */

import client from './client'

// ── Types ─────────────────────────────────────────────────────────────────────

export interface IngestRequest {
  source_type: 'server_path' | 'upload'
  source_path?: string
  upload_file_id?: string
  output_path: string
  project_code: string
  source_label: string
  copyright: string
  domain: string
  language: string
  steps: string[]
}

export interface ActionRequest {
  action: 'approve' | 'reject' | 'submit_review' | 'manual_move' | 'resubmit_script' | 'sandbox_override' | 'cancel'
  step: string
  feedback?: string
  merge_tasks?: Array<{ src_speaker: string; dst_speaker: string }>
  review_items?: ReviewItem[]
  move_src?: string
  move_dst?: string
  save_experience?: boolean   // explicit gate: only write experience if true
}

export interface ReviewItem {
  key: string
  path: string
  audio_url: string
  text: string
  speaker: string
  duration: number
  flags: string[]
  text_edited?: string
  quality_poor: number
  paralanguage_heavy: number
  hardcode_error: number
  too_short: number
}

export interface SSEEvent {
  type: 'thinking' | 'step_start' | 'step_done' | 'progress' | 'pause' | 'error' | 'done'
  ts?: number       // server-side unix timestamp in ms — use as addedAt to survive navigation
  step?: string
  content?: string
  label?: string
  summary?: string
  message?: string
  payload?: Record<string, unknown>
  current?: number
  total?: number
}

// ── REST ─────────────────────────────────────────────────────────────────────

export async function createIngestTask(req: IngestRequest): Promise<{ task_id: string }> {
  const { data } = await client.post<{ task_id: string }>('/api/dataset-ingest', req)
  return data
}

export async function submitAction(taskId: string, req: ActionRequest): Promise<{ ok: boolean }> {
  const { data } = await client.post<{ ok: boolean }>(`/api/dataset-ingest/${taskId}/action`, req)
  return data
}

export async function getDirTree(taskId: string): Promise<unknown> {
  const { data } = await client.get<unknown>(`/api/dataset-ingest/${taskId}/tree`)
  return data
}

export async function getPreview(taskId: string, step: string): Promise<unknown> {
  const { data } = await client.get<unknown>(`/api/dataset-ingest/${taskId}/preview`, {
    params: { step },
  })
  return data
}

export async function getReviewItems(
  taskId: string,
  page: number,
  size: number,
): Promise<{ items: ReviewItem[]; total: number }> {
  const { data } = await client.get<{ items: ReviewItem[]; total: number }>(
    `/api/dataset-ingest/${taskId}/review-items`,
    { params: { page, size } },
  )
  return data
}

export async function cancelTask(taskId: string): Promise<{ ok: boolean }> {
  const { data } = await client.post<{ ok: boolean }>(`/api/dataset-ingest/${taskId}/cancel`)
  return data
}

export async function getMetadataPreview(
  taskId: string,
  limit = 5,
): Promise<{ speakers: Record<string, unknown>[]; total: number }> {
  const { data } = await client.get(`/api/dataset-ingest/${taskId}/metadata-preview`, { params: { limit } })
  return data
}

export async function getAsrPreview(
  taskId: string,
  limit = 15,
): Promise<{ rows: Record<string, unknown>[]; total_rows: number; total_speakers: number }> {
  const { data } = await client.get(`/api/dataset-ingest/${taskId}/asr-preview`, { params: { limit } })
  return data
}

// ── Dataset browser ────────────────────────────────────────────────────────────

export interface BrowseEntry {
  name: string
  path: string
  audio_count?: number // for dirs
  size_bytes?: number // for files
  is_audio?: boolean // for files
  ext?: string
}

export interface BrowseResult {
  dirs: BrowseEntry[]
  files: BrowseEntry[]
  current_path: string
}

export async function browseDirectory(taskId: string, path: string = ''): Promise<BrowseResult> {
  const { data } = await client.get<BrowseResult>(
    `/api/dataset-ingest/${taskId}/browse`,
    { params: { path } },
  )
  return data
}

// ── File upload (used by NewIngestForm upload mode) ───────────────────────────

export async function uploadFile(file: File): Promise<{ file_id: string }> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await client.post<{ file_id: string }>('/api/files/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return data
}

// ── SSE ───────────────────────────────────────────────────────────────────────

export function connectSSE(
  taskId: string,
  onEvent: (event: SSEEvent) => void,
  onThinking: (content: string) => void,
  onError?: (err: Event) => void,
): EventSource {
  /** Open SSE connection to /api/dataset-ingest/stream/{taskId}.
   *  Caller is responsible for calling source.close() on unmount.
   *
   *  Uses Clone Lab's named event channel pattern:
   *   - "message"  channel → structural events (step_start, step_done, progress, pause, error, done)
   *   - "thinking" channel → live thinking token chunks
   */
  const source = new EventSource(`/api/dataset-ingest/stream/${taskId}`)

  // Structural events (step_start, step_done, progress, pause, error, done)
  source.addEventListener('message', (e: MessageEvent) => {
    try {
      const parsed: SSEEvent = JSON.parse(e.data)
      onEvent(parsed)
    } catch {
      onEvent({ type: 'error', message: 'parse error' })
    }
  })

  // Thinking tokens (live streaming text)
  source.addEventListener('thinking', (e: MessageEvent) => {
    try {
      const parsed = JSON.parse(e.data)
      onThinking(parsed.content ?? '')
    } catch { /* ignore */ }
  })

  source.onerror = onError ?? (() => {})

  return source
}
