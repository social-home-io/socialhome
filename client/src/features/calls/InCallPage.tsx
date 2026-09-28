/**
 * InCallPage — the full-screen audio/video UX during an active call (§26).
 *
 * Manages the browser-side WebRTC peer connection, renders the self-view +
 * remote-view, surfaces mic/camera/speaker controls + a duration HUD, and
 * pushes a ``getStats()`` quality sample every 10 s to the backend.
 *
 * The media stack is intentionally single-peer for v1 — group calls fan
 * out at the signalling layer; each pair of participants has its own
 * ``RTCPeerConnection``, signalled over the ``/api/calls/*`` REST routes
 * and the ``call.*`` WS frames.
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute, useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'

interface IceServersResponse { ice_servers: RTCIceServer[] }
interface ActiveCallSummary {
  call_id: string
  call_type: 'audio' | 'video'
}

const durationSeconds  = signal<number>(0)
const micMuted         = signal<boolean>(false)
const cameraOff        = signal<boolean>(false)
const speakerMuted     = signal<boolean>(false)
const quality          = signal<'good' | 'fair' | 'poor'>('good')

function formatDuration(sec: number): string {
  const m = Math.floor(sec / 60).toString().padStart(2, '0')
  const s = (sec % 60).toString().padStart(2, '0')
  return `${m}:${s}`
}

export default function InCallPage() {
  const { params } = useRoute()
  const loc = useLocation()
  const callId = params.callId
  const remoteRef = useRef<HTMLVideoElement>(null)
  const selfRef   = useRef<HTMLVideoElement>(null)
  const pcRef     = useRef<RTCPeerConnection | null>(null)
  const localStreamRef = useRef<MediaStream | null>(null)
  const qualityIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const durationIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    let stopped = false
    const started = Date.now()

    // Reset the module-level signals so a previous call's state doesn't
    // bleed into this mount (this page can be entered multiple times in
    // a session).
    micMuted.value = false
    cameraOff.value = false
    speakerMuted.value = false

    durationIntervalRef.current = setInterval(() => {
      durationSeconds.value = Math.floor((Date.now() - started) / 1000)
    }, 1000)

    // 1. Discover the call's type so we can default the camera state
    //    correctly. ``call_type`` is fixed at offer time on the backend
    //    (spec §26.5); the user's mid-call camera button still flips the
    //    local video track via :func:`toggleCamera`.
    const callTypePromise = (
      api.get('/api/calls/active') as Promise<ActiveCallSummary[]>
    ).then(rows => {
      const me = rows.find(r => r.call_id === callId)
      return me?.call_type ?? 'video'
    }).catch(() => 'video' as const)

    // 2. Pull ICE servers from the backend so STUN/TURN is configured.
    Promise.all([
      api.get('/api/calls/ice-servers') as Promise<IceServersResponse>,
      callTypePromise,
    ]).then(async ([r, callType]) => {
      if (stopped) return
      const pc = new RTCPeerConnection({ iceServers: r.ice_servers ?? [] })
      pcRef.current = pc

      // 3. Acquire local media. We always request audio + video so the
      //    user's mid-call "Enable video" button flips the existing
      //    track instead of needing a fresh permission prompt + a re-
      //    negotiation. Audio calls disable the video track at start
      //    so nothing is transmitted until the user opts in.
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: true,
        video: true,
      })
      if (stopped) { stream.getTracks().forEach(t => t.stop()); return }
      localStreamRef.current = stream
      if (callType === 'audio') {
        stream.getVideoTracks().forEach(t => { t.enabled = false })
        cameraOff.value = true
      }
      stream.getTracks().forEach(t => pc.addTrack(t, stream))
      if (selfRef.current) selfRef.current.srcObject = stream

      // 3. Render the remote stream as tracks arrive.
      pc.ontrack = (evt) => {
        if (!remoteRef.current) return
        const existing = remoteRef.current.srcObject as MediaStream | null
        const ms = existing ?? new MediaStream()
        ms.addTrack(evt.track)
        remoteRef.current.srcObject = ms
      }

      // 4. Trickle ICE candidates to the backend.
      pc.onicecandidate = (evt) => {
        if (!evt.candidate) return
        api.post(`/api/calls/${callId}/ice`, {
          candidate: evt.candidate.toJSON(),
        }).catch(() => { /* best-effort */ })
      }

      // 5. Quality sampler — getStats() every 10 s.
      qualityIntervalRef.current = setInterval(async () => {
        try {
          const stats = await pc.getStats()
          const sample = extractQualitySample(stats)
          await api.post(`/api/calls/${callId}/quality`, sample)
          quality.value = classify(sample)
        } catch { /* swallow */ }
      }, 10_000)
    }).catch((err) => {
      showToast(`Call setup failed: ${(err as Error).message}`, 'error')
    })

    return () => {
      stopped = true
      if (qualityIntervalRef.current) clearInterval(qualityIntervalRef.current)
      if (durationIntervalRef.current) clearInterval(durationIntervalRef.current)
      if (localStreamRef.current) {
        localStreamRef.current.getTracks().forEach(t => t.stop())
      }
      pcRef.current?.close()
    }
  }, [callId])

  const toggleMic = () => {
    const s = localStreamRef.current
    if (!s) return
    s.getAudioTracks().forEach(t => t.enabled = !t.enabled)
    micMuted.value = !micMuted.value
  }
  const toggleCamera = () => {
    const s = localStreamRef.current
    if (!s) return
    s.getVideoTracks().forEach(t => t.enabled = !t.enabled)
    cameraOff.value = !cameraOff.value
  }
  const toggleSpeaker = () => {
    if (!remoteRef.current) return
    remoteRef.current.muted = !remoteRef.current.muted
    speakerMuted.value = remoteRef.current.muted
  }
  const hangup = async () => {
    try { await api.post(`/api/calls/${callId}/hangup`, {}) } catch { /* noop */ }
    loc.route('/dms')
  }

  // Keyboard shortcuts (§26 UX).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === 'm') toggleMic()
      if (e.key.toLowerCase() === 'v') toggleCamera()
      if (e.key === ' ')               { e.preventDefault(); hangup() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div class="sh-incall">
      <header class="sh-incall-header">
        <span class="sh-incall-duration" aria-label="Call duration">
          {formatDuration(durationSeconds.value)}
        </span>
        <span class={`sh-incall-quality sh-q-${quality.value}`}
              aria-label="Connection quality">{quality.value}</span>
      </header>

      <video ref={remoteRef} class="sh-incall-remote" autoplay playsinline />
      <video ref={selfRef}   class="sh-incall-self"   autoplay playsinline muted />

      <footer class="sh-incall-controls">
        <Button class={micMuted.value ? 'sh-ctrl-off' : ''}
                onClick={toggleMic}
                aria-label={micMuted.value ? 'Unmute mic' : 'Mute mic'}>
          {micMuted.value ? '🎤🚫' : '🎤'}
        </Button>
        <Button class={cameraOff.value ? 'sh-ctrl-off' : ''}
                onClick={toggleCamera}
                aria-label={cameraOff.value ? 'Turn camera on' : 'Turn camera off'}>
          {cameraOff.value ? '🎥🚫' : '🎥'}
        </Button>
        <Button class={speakerMuted.value ? 'sh-ctrl-off' : ''}
                onClick={toggleSpeaker}
                aria-label={speakerMuted.value ? 'Unmute speaker' : 'Mute speaker'}>
          {speakerMuted.value ? '🔊🚫' : '🔊'}
        </Button>
        <Button class="sh-hangup" onClick={hangup} aria-label="Hang up">🔴</Button>
      </footer>
    </div>
  )
}

interface QualitySample {
  rtt_ms?: number | null
  jitter_ms?: number | null
  loss_pct?: number | null
  audio_bitrate?: number | null
  video_bitrate?: number | null
  sampled_at?: number
}

function extractQualitySample(stats: RTCStatsReport): QualitySample {
  let rtt: number | null = null
  let jitter: number | null = null
  let loss: number | null = null
  let audioBitrate: number | null = null
  let videoBitrate: number | null = null
  stats.forEach((report: Record<string, unknown>) => {
    if (report.type === 'candidate-pair' && report.state === 'succeeded') {
      const r = report as { currentRoundTripTime?: number }
      if (typeof r.currentRoundTripTime === 'number') {
        rtt = Math.round(r.currentRoundTripTime * 1000)
      }
    }
    if (report.type === 'inbound-rtp') {
      const r = report as {
        kind?: string, jitter?: number,
        packetsLost?: number, packetsReceived?: number,
        bytesReceived?: number, timestamp?: number,
      }
      if (typeof r.jitter === 'number') {
        jitter = Math.round(r.jitter * 1000)
      }
      if (r.packetsLost != null && r.packetsReceived != null && r.packetsReceived > 0) {
        loss = Math.round(100 * r.packetsLost / (r.packetsLost + r.packetsReceived) * 10) / 10
      }
      if (r.kind === 'audio' && typeof r.bytesReceived === 'number') {
        audioBitrate = r.bytesReceived * 8
      }
      if (r.kind === 'video' && typeof r.bytesReceived === 'number') {
        videoBitrate = r.bytesReceived * 8
      }
    }
  })
  return {
    rtt_ms: rtt, jitter_ms: jitter, loss_pct: loss,
    audio_bitrate: audioBitrate, video_bitrate: videoBitrate,
    sampled_at: Math.floor(Date.now() / 1000),
  }
}

function classify(s: QualitySample): 'good' | 'fair' | 'poor' {
  const loss = s.loss_pct ?? 0
  const rtt  = s.rtt_ms   ?? 0
  if (loss > 5 || rtt > 300) return 'poor'
  if (loss > 1 || rtt > 150) return 'fair'
  return 'good'
}
