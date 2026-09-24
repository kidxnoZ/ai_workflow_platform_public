import { useRef, useState } from 'react'
import { Button, Space } from 'antd'
import { PlayCircleOutlined, PauseCircleOutlined, DownloadOutlined } from '@ant-design/icons'

export default function AudioPlayer({ url, label }: { url: string; label?: string }) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)

  const toggle = () => {
    const el = audioRef.current
    if (!el) return
    if (playing) {
      el.pause()
      setPlaying(false)
    } else {
      el.play()
      setPlaying(true)
    }
  }

  return (
    <Space size={4}>
      <audio ref={audioRef} src={url} onEnded={() => setPlaying(false)} style={{ display: 'none' }} />
      <Button
        type="text"
        size="small"
        icon={playing ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
        onClick={toggle}
      />
      <a href={url} download>
        <Button type="text" size="small" icon={<DownloadOutlined />} />
      </a>
    </Space>
  )
}
