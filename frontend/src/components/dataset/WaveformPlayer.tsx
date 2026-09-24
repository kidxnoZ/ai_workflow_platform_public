/**
 * frontend/src/components/dataset/WaveformPlayer.tsx
 *
 * Audition-style waveform display with dBFS scale overlay and threshold line.
 * Uses wavesurfer.js for rendering.
 *
 * Scale design:
 * - WaveSurfer renders a BIPOLAR waveform: center = silence (−∞ dBFS),
 *   outer edges (top/bottom) = peak amplitude (0 dBFS).
 * - Y axis uses AMPLITUDE-LINEAR spacing to match WaveSurfer's actual rendering:
 *   dbToY(db) = (1 − 10^(db/20)) × HEIGHT/2
 * - Marks below −24 dBFS cluster within ~3 px of center so they're omitted.
 * - Grid lines and threshold line are mirrored to the negative (lower) half.
 */
import React, { useEffect, useRef, useCallback, useState } from 'react'
import WaveSurfer from 'wavesurfer.js'
import { Button, Tooltip } from 'antd'
import { PauseOutlined, CaretRightOutlined } from '@ant-design/icons'

// ── Constants ────────────────────────────────────────────────────────────────

const WAVEFORM_HEIGHT = 96       // total px (top half = positive amp, bottom = negative amp)
const SCALE_WIDTH = 42           // px for the dBFS label column

// Amplitude-linear positions for these marks are visually distinguishable.
// −24 dBFS is already at 93% of the half-height; below that all marks crowd within 3 px.
const DB_SCALE_MARKS = [0, -3, -6, -12, -18, -24]

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Convert dBFS → Y pixel for the TOP half of a bipolar waveform.
 *
 *   0 dBFS  (amplitude 1.0)  → y = 0          (outer edge)
 *   −6 dBFS (amplitude 0.5)  → y = HEIGHT/2 × 0.499  ≈ 24 px
 *   −∞ dBFS (amplitude 0)    → y = HEIGHT/2            (center)
 *
 * Mirror: y_bottom = WAVEFORM_HEIGHT − y_top
 */
function dbToY(db: number): number {
  if (db >= 0) return 0
  const amplitude = Math.pow(10, db / 20)
  return Math.round((1 - amplitude) * (WAVEFORM_HEIGHT / 2))
}

function formatDb(db: number): string {
  return db === 0 ? '0' : `${db}`
}

// ── Scale SVG overlay ─────────────────────────────────────────────────────────

function DbScaleOverlay({
  thresholdDb,
  width,
}: {
  thresholdDb: number
  width: number
}): React.ReactElement {
  const half = WAVEFORM_HEIGHT / 2
  const threshY = dbToY(thresholdDb)
  const threshYMirror = WAVEFORM_HEIGHT - threshY

  return (
    <svg
      width={width}
      height={WAVEFORM_HEIGHT}
      style={{ position: 'absolute', left: SCALE_WIDTH, top: 0, pointerEvents: 'none' }}
    >
      {/* Symmetric scale grid lines (top + mirrored bottom) */}
      {DB_SCALE_MARKS.map((db) => {
        const y = dbToY(db)
        const yMirror = WAVEFORM_HEIGHT - y
        const isEdge = db === 0
        const stroke = isEdge ? '#595959' : '#3a3a3a'
        const sw = isEdge ? 1 : 0.5
        return (
          <g key={db}>
            <line x1={0} y1={y} x2={width} y2={y} stroke={stroke} strokeWidth={sw} />
            {y !== yMirror && (
              <line x1={0} y1={yMirror} x2={width} y2={yMirror} stroke={stroke} strokeWidth={sw} />
            )}
          </g>
        )
      })}

      {/* Center line: zero crossing = silence = −∞ dBFS */}
      <line x1={0} y1={half} x2={width} y2={half} stroke="#4a4a4a" strokeWidth={1} />

      {/* Threshold line: draw at both halves (they coincide when threshold is near −∞) */}
      {thresholdDb > -80 && thresholdDb < 0 && (
        <g>
          <line x1={0} y1={threshY} x2={width} y2={threshY}
            stroke="#ff4d4f" strokeWidth={1.5} strokeDasharray="4 3" />
          {threshY !== threshYMirror && (
            <line x1={0} y1={threshYMirror} x2={width} y2={threshYMirror}
              stroke="#ff4d4f" strokeWidth={1.5} strokeDasharray="4 3" />
          )}
          {/* Label pinned so it stays visible even when threshold ≈ center */}
          <rect x={4} y={Math.max(2, threshY - 9)} width={54} height={16} rx={2}
            fill="rgba(255,77,79,0.85)" />
          <text x={8} y={Math.max(12, threshY + 3)} fill="#fff" fontSize={9} fontFamily="monospace">
            {thresholdDb.toFixed(1)} dB
          </text>
        </g>
      )}
    </svg>
  )
}

// ── Scale labels (left column) ────────────────────────────────────────────────

function ScaleLabels(): React.ReactElement {
  const half = WAVEFORM_HEIGHT / 2
  return (
    <div style={{
      width: SCALE_WIDTH,
      height: WAVEFORM_HEIGHT,
      position: 'relative',
      flexShrink: 0,
      background: '#141414',
      borderRight: '1px solid #303030',
    }}>
      {/* Top-half labels */}
      {DB_SCALE_MARKS.map((db) => {
        const y = dbToY(db)
        return (
          <div key={db} style={{
            position: 'absolute',
            top: y - 7,
            right: 4,
            fontSize: 9,
            color: '#8c8c8c',
            fontFamily: 'monospace',
            lineHeight: '14px',
            whiteSpace: 'nowrap',
          }}>
            {formatDb(db)}
          </div>
        )
      })}
      {/* Center: zero crossing = −∞ */}
      <div style={{
        position: 'absolute',
        top: half - 7,
        right: 4,
        fontSize: 9,
        color: '#595959',
        fontFamily: 'monospace',
        lineHeight: '14px',
      }}>
        −∞
      </div>
    </div>
  )
}

// ── Single waveform track ─────────────────────────────────────────────────────

function WaveTrack({
  url,
  thresholdDb,
  label,
  color,
  progressColor,
  onReady,
}: {
  url: string
  thresholdDb: number
  label: string
  color: string
  progressColor: string
  onReady?: (ws: WaveSurfer) => void
}): React.ReactElement {
  const containerRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WaveSurfer | null>(null)
  const [duration, setDuration] = useState(0)
  const [currentTime, setCurrentTime] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [scaleWidth, setScaleWidth] = useState(300)

  useEffect(() => {
    if (!containerRef.current) return
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 300
      setScaleWidth(w)
    })
    ro.observe(containerRef.current)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    if (!containerRef.current) return
    const ws = WaveSurfer.create({
      container: containerRef.current,
      waveColor: color,
      progressColor: progressColor,
      height: WAVEFORM_HEIGHT,
      normalize: true,
      barWidth: 1,
      barGap: 0,
      interact: true,
      backend: 'WebAudio',
    })

    ws.load(url)
    ws.on('ready', () => {
      setDuration(ws.getDuration())
      onReady?.(ws)
    })
    ws.on('audioprocess', () => setCurrentTime(ws.getCurrentTime()))
    ws.on('play', () => setIsPlaying(true))
    ws.on('pause', () => setIsPlaying(false))
    ws.on('finish', () => setIsPlaying(false))

    wsRef.current = ws
    return () => {
      ws.destroy()
      wsRef.current = null
    }
  }, [url]) // eslint-disable-line react-hooks/exhaustive-deps

  const togglePlay = useCallback(() => {
    wsRef.current?.playPause()
  }, [])

  const fmt = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`

  return (
    <div style={{ marginBottom: 2 }}>
      {/* Label row */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2,
        fontSize: 10, color: '#8c8c8c',
      }}>
        <span style={{
          width: 36, height: 14, borderRadius: 2, background: color,
          display: 'inline-block', flexShrink: 0,
        }} />
        <span style={{ fontWeight: 600, color: '#d9d9d9' }}>{label}</span>
        <span style={{ marginLeft: 'auto', fontFamily: 'monospace' }}>
          {fmt(currentTime)} / {fmt(duration)}
        </span>
        <Button
          type="text" size="small"
          icon={isPlaying ? <PauseOutlined /> : <CaretRightOutlined />}
          onClick={togglePlay}
          style={{ color: '#8c8c8c', padding: '0 4px', height: 18 }}
        />
      </div>
      {/* Waveform + scale */}
      <div style={{
        display: 'flex',
        background: '#1a1a1a',
        borderRadius: 3,
        overflow: 'hidden',
        border: '1px solid #303030',
      }}>
        <ScaleLabels />
        <div style={{ flex: 1, position: 'relative' }}>
          <div ref={containerRef} />
          <DbScaleOverlay thresholdDb={thresholdDb} width={scaleWidth} />
        </div>
      </div>
    </div>
  )
}

// ── Public component ──────────────────────────────────────────────────────────

export interface WaveformCompareProps {
  beforeUrl: string
  afterUrl: string
  thresholdDb?: number
  label?: string
}

export default function WaveformCompare({
  beforeUrl,
  afterUrl,
  thresholdDb = -55,
  label,
}: WaveformCompareProps): React.ReactElement {
  return (
    <div>
      {label && (
        <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 4 }}>
          {label}
          <Tooltip title={`红色虚线 = 静音阈值 ${thresholdDb.toFixed(1)} dBFS（低于此值视为底噪）`}>
            <span style={{
              marginLeft: 6, fontSize: 10, color: '#ff4d4f', cursor: 'help',
              border: '1px solid #ff4d4f', borderRadius: 2, padding: '0 3px',
            }}>
              阈值 {thresholdDb.toFixed(1)} dB
            </span>
          </Tooltip>
        </div>
      )}
      <WaveTrack
        url={beforeUrl}
        thresholdDb={thresholdDb}
        label="处理前"
        color="#4a9eff"
        progressColor="#1677ff"
      />
      <WaveTrack
        url={afterUrl}
        thresholdDb={thresholdDb}
        label="处理后"
        color="#73d13d"
        progressColor="#52c41a"
      />
    </div>
  )
}
