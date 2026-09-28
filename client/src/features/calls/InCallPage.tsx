/**
 * InCallPage — the full-screen audio/video UX during an active call (§26).
 *
 * Renders the live session owned by :mod:`./callSession` (which creates
 * the ``RTCPeerConnection`` and runs the SDP offer/answer + trickle-ICE
 * exchange over ``/api/calls/*`` and the ``call.*`` WS frames): self-view
 * + remote-view, mic/camera/speaker controls, a status line while the
 * call is ringing / connecting, a duration HUD once media flows, and a
 * ``getStats()`` quality sample pushed every 10 s.
 *
 * The session starts before this page mounts (in the call picker or the
 * ringing dialog). Landing here without one — a reload, a stale link —
 * shows an honest "not connected on this device" state instead of an
 * empty black screen. Leaving the page hangs up.
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute, useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import {
  callConversation, callEndReason, callId as sessionCallId, callPhase,
  callType, getPeerConnection, hangupCall, hasCamera, isCallLive, localStream,
  remoteStream, resetCall, type CallPhase,
} from './callSession'

const durationSeconds  = signal<number>(0)
const micMuted         = signal<boolean>(false)
const cameraOff        = signal<boolean>(false)
const speakerMuted     = signal<boolean>(false)
const quality          = signal<'good' | 'fair' | 'poor'>('good')

const STATUS_COPY: Partial<Record<CallPhase, string>> = {
  starting:     'Starting call…',
  ringing:      'Calling…',
  connecting:   'Connecting…',
  reconnecting: 'Reconnecting…',
}

function formatDuration(sec: number): string {
  const m = Math.floor(sec / 60).toString().padStart(2, '0')
  const s = (sec % 60).toString().padStart(2, '0')
  return `${m}:${s}`
}

function videoOn(stream: MediaStream | null): boolean {
  return stream?.getVideoTracks().some(t => t.enabled) ?? false
}

export default function InCallPage() {
  const { params } = useRoute()
  const loc = useLocation()
  const callId = params.callId
  const remoteRef = useRef<HTMLVideoElement>(null)
  const selfRef   = useRef<HTMLVideoElement>(null)
  const convRef   = useRef<string | null>(null)

  const ours = sessionCallId.value === callId
  const phase: CallPhase = ours ? callPhase.value : 'idle'
  if (ours && callConversation.value) convRef.current = callConversation.value
  const local  = ours ? localStream.value : null
  const remote = ours ? remoteStream.value : null

  const leave = () => {
    const conv = convRef.current
    resetCall()
    loc.route(conv ? `/dms/${conv}` : '/dms')
  }
  // "Not connected on this device" (a reload, a stale link): the call may
  // still be ringing or open on the backend, with the other side waiting
  // on us. Leaving ends it there too. Best-effort — an already-ended
  // call answers 404, which is fine.
  const abandon = () => {
    if (callId) void api.post(`/api/calls/${callId}/hangup`, {}).catch(() => {})
    leave()
  }

  // Reset the per-call controls on every entry; hang up when the user
  // navigates away mid-call so the other side isn't left talking to no one.
  useEffect(() => {
    micMuted.value = false
    speakerMuted.value = false
    quality.value = 'good'
    durationSeconds.value = 0
    return () => {
      if (sessionCallId.value === callId && isCallLive()) void hangupCall()
    }
  }, [callId])

  // Attach the streams to the <video> elements as the session produces them.
  useEffect(() => {
    if (selfRef.current) selfRef.current.srcObject = local
    cameraOff.value = !videoOn(local)
  }, [local])
  useEffect(() => {
    if (remoteRef.current) remoteRef.current.srcObject = remote
  }, [remote])

  // Duration + quality sampler run only while media is flowing.
  const live = phase === 'connected' || phase === 'reconnecting'
  useEffect(() => {
    if (!live) return
    const started = Date.now() - durationSeconds.value * 1000
    const tick = setInterval(() => {
      durationSeconds.value = Math.floor((Date.now() - started) / 1000)
    }, 1000)
    const sampler = setInterval(async () => {
      const pc = getPeerConnection()
      if (!pc) return
      try {
        const sample = extractQualitySample(await pc.getStats())
        await api.post(`/api/calls/${callId}/quality`, sample)
        quality.value = classify(sample)
      } catch { /* swallow */ }
    }, 10_000)
    return () => { clearInterval(tick); clearInterval(sampler) }
  }, [live, callId])

  // The call ended (either side hung up / declined): say why, if the
  // other side ended it, and go back to the thread.
  useEffect(() => {
    if (phase !== 'ended') return
    if (callEndReason.value) showToast(callEndReason.value, 'info')
    leave()
  }, [phase])

  const toggleMic = () => {
    const s = localStream.value
    if (!s) return
    s.getAudioTracks().forEach(t => t.enabled = !t.enabled)
    micMuted.value = !micMuted.value
  }
  const toggleCamera = () => {
    const s = localStream.value
    if (!s || !hasCamera.value) return
    s.getVideoTracks().forEach(t => t.enabled = !t.enabled)
    cameraOff.value = !videoOn(s)
  }
  const toggleSpeaker = () => {
    if (!remoteRef.current) return
    remoteRef.current.muted = !remoteRef.current.muted
    speakerMuted.value = remoteRef.current.muted
  }
  // Tearing the session down flips the phase to ``ended``; the effect
  // above routes back to the thread.
  const hangup = () => { void hangupCall() }

  // Keyboard shortcuts (§26 UX). Skipped while a control has focus so
  // Space / Enter keep meaning "press the focused button".
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && e.target.closest('button, input, textarea')) return
      if (e.key.toLowerCase() === 'm') toggleMic()
      if (e.key.toLowerCase() === 'v') toggleCamera()
      if (e.key === ' ')               { e.preventDefault(); hangup() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (phase === 'idle' || phase === 'failed') {
    const message = phase === 'failed'
      ? (callEndReason.value ?? "The call couldn't be connected.")
      : "This call isn't connected on this device — it may have ended, or "
        + 'the page reloaded. Going back ends it, so nobody is left waiting.'
    return (
      <div class="sh-incall sh-incall--closed" role="alert">
        <div class="sh-incall-closed-card">
          <strong>{phase === 'failed' ? 'Call failed' : 'Call not connected'}</strong>
          <p>{message}</p>
          <Button onClick={phase === 'failed' ? leave : abandon}>Back to chats</Button>
        </div>
      </div>
    )
  }

  const status = STATUS_COPY[phase]
  return (
    <div class="sh-incall">
      <header class="sh-incall-header">
        <span class="sh-incall-duration" aria-label="Call duration">
          {live ? formatDuration(durationSeconds.value) : ''}
        </span>
        {live && (
          <span class={`sh-incall-quality sh-q-${quality.value}`}
                aria-label="Connection quality">{quality.value}</span>
        )}
      </header>

      <video ref={remoteRef} class="sh-incall-remote" autoplay playsinline />
      {status && (
        <p class="sh-incall-status" role="status" aria-live="polite">{status}</p>
      )}
      <video ref={selfRef}   class="sh-incall-self"   autoplay playsinline muted />

      <footer class="sh-incall-controls">
        <Button class={micMuted.value ? 'sh-ctrl-off' : ''}
                onClick={toggleMic}
                aria-pressed={micMuted.value}
                aria-label={micMuted.value ? 'Unmute mic' : 'Mute mic'}>
          {micMuted.value ? '🎤🚫' : '🎤'}
        </Button>
        <Button class={cameraOff.value ? 'sh-ctrl-off' : ''}
                onClick={toggleCamera}
                disabled={!hasCamera.value}
                aria-pressed={cameraOff.value}
                aria-label={!hasCamera.value
                  ? (callType.value === 'audio' ? 'Camera is off in audio calls' : 'No camera available')
                  : cameraOff.value ? 'Turn camera on' : 'Turn camera off'}>
          {cameraOff.value ? '🎥🚫' : '🎥'}
        </Button>
        <Button class={speakerMuted.value ? 'sh-ctrl-off' : ''}
                onClick={toggleSpeaker}
                aria-pressed={speakerMuted.value}
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
