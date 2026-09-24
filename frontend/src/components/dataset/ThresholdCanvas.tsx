/**
 * ThresholdCanvas.tsx
 *
 * Two-panel silence-threshold visualisation:
 *   Left  (文件起始段): head window — [silence → speech onset]
 *   Right (文件末尾段): tail window — [speech offset → silence]
 *
 * Both panels share the same Y scale (dBFS-linear) and the same threshold line.
 * A center divider makes clear the two panels are separate parts of the file.
 */
import React, { useEffect, useRef, useCallback, useState } from 'react'
import { Slider, Typography } from 'antd'
import { CaretRightOutlined, PauseOutlined } from '@ant-design/icons'

const { Text } = Typography

const DB_MIN = -80
const CANVAS_HEIGHT = 200
const HALF = CANVAS_HEIGHT / 2
const SCALE_WIDTH = 64
const DB_MARKS = [0, -6, -12, -18, -24, -36, -48, -60]

function dbToY(db: number): number {
  if (db >= 0) return 0
  if (db <= DB_MIN) return HALF
  return Math.round((Math.abs(db) / Math.abs(DB_MIN)) * HALF)
}
function yToDb(y: number): number {
  return -(Math.max(0, Math.min(HALF, y)) / HALF) * Math.abs(DB_MIN)
}

// ── Shared dBFS scale column ──────────────────────────────────────────────────

function ScaleLabels(): React.ReactElement {
  return (
    <div style={{
      width: SCALE_WIDTH, height: CANVAS_HEIGHT, position: 'relative',
      flexShrink: 0, background: '#f0f0f0', borderRight: '1px solid #d0d0d0',
    }}>
      <div style={{
        position: 'absolute', top: 1, left: 0, right: 0,
        textAlign: 'center', fontSize: 8, color: '#888', fontFamily: 'monospace',
      }}>dBFS</div>
      {DB_MARKS.map(db => {
        const y = dbToY(db)
        const yMirror = CANVAS_HEIGHT - y
        return (
          <React.Fragment key={db}>
            <div style={{ position: 'absolute', top: y - 7, right: 5, fontSize: 10, color: '#444', fontFamily: 'monospace', whiteSpace: 'nowrap' }}>
              {db === 0 ? '0' : `${db}`}
            </div>
            {y !== yMirror && (
              <div style={{ position: 'absolute', top: yMirror - 7, right: 5, fontSize: 10, color: '#444', fontFamily: 'monospace', whiteSpace: 'nowrap' }}>
                {db === 0 ? '0' : `${db}`}
              </div>
            )}
          </React.Fragment>
        )
      })}
      <div style={{ position: 'absolute', top: HALF - 7, right: 5, fontSize: 10, color: '#888', fontFamily: 'monospace' }}>−∞</div>
    </div>
  )
}

// ── Single waveform canvas panel ──────────────────────────────────────────────

function WavePanel({
  envelope,
  transitionPos,
  transitionLabel,
  thresholdDb,
  playProgress,
  onThresholdDrag,
  onSeek,
  canvasWidth,
}: {
  envelope: number[]
  transitionPos?: number
  transitionLabel: string
  thresholdDb: number
  playProgress?: number
  onThresholdDrag?: (db: number) => void
  onSeek?: (progress: number) => void
  canvasWidth: number
}): React.ReactElement {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const dragging = useRef(false)

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const W = canvas.width

    ctx.fillStyle = '#f0f0f0'
    ctx.fillRect(0, 0, W, CANVAS_HEIGHT)

    // Grid
    for (const db of DB_MARKS) {
      const y = dbToY(db); const ym = CANVAS_HEIGHT - y
      ctx.strokeStyle = db === 0 ? '#b0b0b0' : '#d8d8d8'
      ctx.lineWidth = db === 0 ? 1 : 0.5
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke()
      if (y !== ym) { ctx.beginPath(); ctx.moveTo(0, ym); ctx.lineTo(W, ym); ctx.stroke() }
    }
    ctx.strokeStyle = '#c0c0c0'; ctx.lineWidth = 1
    ctx.beginPath(); ctx.moveTo(0, HALF); ctx.lineTo(W, HALF); ctx.stroke()

    // Waveform bars
    if (envelope.length > 0) {
      const bw = W / envelope.length
      ctx.fillStyle = '#4a7fc1'
      for (let i = 0; i < envelope.length; i++) {
        const x = i * bw; const y = dbToY(envelope[i])
        ctx.fillRect(x, y, Math.max(1, bw - 0.5), HALF - y)
        ctx.fillRect(x, HALF, Math.max(1, bw - 0.5), CANVAS_HEIGHT - y - HALF)
      }
    }

    // Threshold line (both halves)
    if (thresholdDb > DB_MIN && thresholdDb < 0) {
      const yT = dbToY(thresholdDb); const yTm = CANVAS_HEIGHT - yT
      ctx.strokeStyle = '#ff4d4f'; ctx.lineWidth = 1.5
      ctx.setLineDash([4, 3])
      ctx.beginPath(); ctx.moveTo(0, yT); ctx.lineTo(W, yT); ctx.stroke()
      if (yT !== yTm) { ctx.beginPath(); ctx.moveTo(0, yTm); ctx.lineTo(W, yTm); ctx.stroke() }
      ctx.setLineDash([])
      ctx.fillStyle = 'rgba(255,77,79,0.9)'
      ctx.fillRect(2, Math.max(2, yT - 9), 58, 16)
      ctx.fillStyle = '#fff'; ctx.font = '9px monospace'
      ctx.fillText(`${thresholdDb.toFixed(1)} dBFS`, 6, Math.max(11, yT + 3))
    }

    // Transition marker
    if (transitionPos != null && envelope.length > 0) {
      const xM = Math.round((transitionPos / envelope.length) * W)
      ctx.strokeStyle = 'rgba(80,80,80,0.5)'; ctx.lineWidth = 1
      ctx.setLineDash([3, 4])
      ctx.beginPath(); ctx.moveTo(xM, 0); ctx.lineTo(xM, CANVAS_HEIGHT); ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = 'rgba(80,80,80,0.6)'; ctx.font = '8px monospace'
      ctx.fillText(transitionLabel, Math.max(2, xM - 14), 10)
    }

    // Playback progress line
    if (playProgress != null && playProgress > 0) {
      ctx.strokeStyle = 'rgba(0,0,0,0.5)'; ctx.lineWidth = 1.5
      ctx.beginPath(); ctx.moveTo(Math.round(playProgress * W), 0); ctx.lineTo(Math.round(playProgress * W), CANVAS_HEIGHT); ctx.stroke()
    }
  }, [envelope, thresholdDb, transitionPos, playProgress])

  useEffect(() => { draw() }, [draw, canvasWidth])

  const getY = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = canvasRef.current!.getBoundingClientRect()
    return (e.clientY - rect.top) * (CANVAS_HEIGHT / rect.height)
  }
  const getProgress = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = canvasRef.current!.getBoundingClientRect()
    return Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
  }

  return (
    <canvas
      ref={canvasRef}
      width={canvasWidth}
      height={CANVAS_HEIGHT}
      style={{ display: 'block', width: '100%', height: CANVAS_HEIGHT, cursor: 'crosshair', border: '1px solid #d0d0d0', borderLeft: 'none' }}
      onMouseDown={e => {
        const y = getY(e); const yT = dbToY(thresholdDb); const yTm = CANVAS_HEIGHT - yT
        if (Math.abs(y - yT) < 10 || Math.abs(y - yTm) < 10) { dragging.current = true; e.preventDefault() }
      }}
      onMouseMove={e => {
        if (!dragging.current || !onThresholdDrag) return
        const y = getY(e)
        onThresholdDrag(Math.max(-80, Math.min(-10, Math.round(yToDb(Math.min(HALF, Math.max(0, y)))))))
      }}
      onMouseUp={e => {
        if (dragging.current) { dragging.current = false; return }
        if (onSeek) onSeek(getProgress(e))
      }}
      onMouseLeave={() => { dragging.current = false }}
    />
  )
}

// ── Public component ──────────────────────────────────────────────────────────

export interface ThresholdCanvasProps {
  headEnvelope: number[]
  headTransitionPos?: number
  tailEnvelope: number[]
  tailTransitionPos?: number
  thresholdDb: number
  onThresholdChange: (db: number) => void
  segmentUrl?: string
  tailSegmentUrl?: string
  peakDb?: number
  meanDb?: number
  sampleRate?: number
}

export default function ThresholdCanvas({
  headEnvelope, headTransitionPos,
  tailEnvelope, tailTransitionPos,
  thresholdDb, onThresholdChange,
  segmentUrl, tailSegmentUrl,
  peakDb, meanDb, sampleRate,
}: ThresholdCanvasProps): React.ReactElement {
  const headContainerRef = useRef<HTMLDivElement>(null)
  const tailContainerRef = useRef<HTMLDivElement>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const tailAudioRef = useRef<HTMLAudioElement | null>(null)
  const [headWidth, setHeadWidth] = useState(300)
  const [tailWidth, setTailWidth] = useState(300)
  const [isPlaying, setIsPlaying] = useState(false)
  const [isTailPlaying, setIsTailPlaying] = useState(false)
  const [playProgress, setPlayProgress] = useState(0)

  useEffect(() => {
    const observe = (el: HTMLDivElement | null, set: (w: number) => void) => {
      if (!el) return () => {}
      const ro = new ResizeObserver(entries => set(Math.max(60, Math.round(entries[0]?.contentRect.width ?? 200))))
      ro.observe(el); return () => ro.disconnect()
    }
    const d1 = observe(headContainerRef.current, setHeadWidth)
    const d2 = observe(tailContainerRef.current, setTailWidth)
    return () => { d1(); d2() }
  }, [])

  const handleSeek = useCallback((progress: number) => {
    if (audioRef.current && segmentUrl) {
      audioRef.current.currentTime = progress * (audioRef.current.duration || 0)
      setPlayProgress(progress)
    }
  }, [segmentUrl])

  return (
    <div>
      {/* Stats row — numbers only */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 6, fontSize: 11, color: '#8c8c8c', alignItems: 'center' }}>
        {peakDb != null && <span>峰值 <Text style={{ color: '#444', fontSize: 11 }}>{peakDb.toFixed(1)} dBFS</Text></span>}
        {meanDb != null && <span>均值 <Text style={{ color: '#444', fontSize: 11 }}>{meanDb.toFixed(1)} dBFS</Text></span>}
        {sampleRate != null && <span>采样率 <Text style={{ color: '#444', fontSize: 11 }}>{sampleRate} Hz</Text></span>}
      </div>

      {/* Panel labels — each with its own play button */}
      <div style={{ display: 'flex', paddingLeft: SCALE_WIDTH, marginBottom: 2 }}>
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6, fontSize: 10, color: '#666' }}>
          文件起始段
          {segmentUrl && (
            <button
              onClick={() => {
                if (!audioRef.current) {
                  audioRef.current = new Audio(segmentUrl)
                  audioRef.current.onended = () => { setIsPlaying(false); setPlayProgress(0) }
                  audioRef.current.ontimeupdate = () => {
                    const a = audioRef.current!
                    setPlayProgress(a.currentTime / (a.duration || 1))
                  }
                }
                if (isPlaying) { audioRef.current.pause(); setIsPlaying(false) }
                else { audioRef.current.play(); setIsPlaying(true) }
              }}
              style={{ display: 'flex', alignItems: 'center', gap: 3, background: 'none', border: '1px solid #bbb', borderRadius: 3, color: '#555', cursor: 'pointer', padding: '1px 6px', fontSize: 10 }}
            >
              {isPlaying ? <PauseOutlined /> : <CaretRightOutlined />}
              {isPlaying ? '暂停' : '播放'}
            </button>
          )}
        </div>
        <div style={{ width: 13 }} />
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6, fontSize: 10, color: '#666' }}>
          文件末尾段
          {/* Show note when tail transition is very close to right edge (no trailing silence) */}
          {tailTransitionPos != null && tailEnvelope.length > 0 && tailTransitionPos > tailEnvelope.length * 0.85
            ? <span style={{ color: '#aaa', fontSize: 9 }}>（末尾底噪不足）</span>
            : null}
          {tailSegmentUrl && (
            <button
              onClick={() => {
                if (!tailAudioRef.current) {
                  tailAudioRef.current = new Audio(tailSegmentUrl)
                  tailAudioRef.current.onended = () => setIsTailPlaying(false)
                }
                if (isTailPlaying) { tailAudioRef.current.pause(); setIsTailPlaying(false) }
                else { tailAudioRef.current.play(); setIsTailPlaying(true) }
              }}
              style={{ display: 'flex', alignItems: 'center', gap: 3, background: 'none', border: '1px solid #bbb', borderRadius: 3, color: '#555', cursor: 'pointer', padding: '1px 6px', fontSize: 10 }}
            >
              {isTailPlaying ? <PauseOutlined /> : <CaretRightOutlined />}
              {isTailPlaying ? '暂停' : '播放'}
            </button>
          )}
        </div>
      </div>

      {/* Two canvases + shared scale */}
      <div style={{ display: 'flex', userSelect: 'none' }}>
        <ScaleLabels />

        {/* Head canvas */}
        <div ref={headContainerRef} style={{ flex: 1, minWidth: 0 }}>
          <WavePanel
            envelope={headEnvelope}
            transitionPos={headTransitionPos}
            transitionLabel="语音起"
            thresholdDb={thresholdDb}
            playProgress={playProgress}
            onThresholdDrag={onThresholdChange}
            onSeek={handleSeek}
            canvasWidth={headWidth}
          />
        </div>

        {/* Center divider */}
        <div style={{ width: 1, background: '#aaa', margin: '0 6px', flexShrink: 0 }} />

        {/* Tail canvas */}
        <div ref={tailContainerRef} style={{ flex: 1, minWidth: 0 }}>
          <WavePanel
            envelope={tailEnvelope}
            transitionPos={tailTransitionPos}
            transitionLabel="语音末"
            thresholdDb={thresholdDb}
            onThresholdDrag={onThresholdChange}
            canvasWidth={tailWidth}
          />
        </div>
      </div>

      {/* Shared threshold slider */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 8 }}>
        <Text style={{ fontSize: 11, color: '#8c8c8c', flexShrink: 0 }}>静音阈值</Text>
        <Slider min={-80} max={-10} step={1} value={thresholdDb} onChange={v => onThresholdChange(v as number)} style={{ flex: 1 }} tooltip={{ formatter: v => `${v} dBFS` }} />
        <Text style={{ fontSize: 12, fontFamily: 'monospace', color: '#ff4d4f', flexShrink: 0, width: 76 }}>
          {thresholdDb.toFixed(0)} dBFS
        </Text>
      </div>
    </div>
  )
}
