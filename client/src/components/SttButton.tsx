/**
 * SttButton — push-to-talk microphone (§platform/stt).
 *
 * Captures audio from the user's microphone, downsamples to 16 kHz
 * PCM16 little-endian mono inside an AudioWorklet, and streams chunks
 * over a dedicated WebSocket to `/api/stt/stream`. On release, waits
 * for the server's `{type:"final",text}` frame and forwards the text
 * to the parent via `onText`.
 *
 * Hidden automatically after the first failed attempt when the server
 * reports no STT support — so standalone mode (which has no STT in v1)
 * quietly degrades instead of showing a broken button.
 */
import type preact from 'preact'
import { signal, useSignal } from '@preact/signals'
import { token } from '@/store/auth'
import { t } from '@/i18n/i18n'

const TARGET_SAMPLE_RATE = 16000

type State = 'idle' | 'recording' | 'uploading' | 'error'

const unsupported = signal(false)

interface SttButtonProps {
  onText: (text: string) => void
  language?: string
  disabled?: boolean
  className?: string
}

interface ActiveRecording {
  ws: WebSocket
  ctx: AudioContext
  stream: MediaStream
  worklet: AudioWorkletNode
  source: MediaStreamAudioSourceNode
}

export function SttButton({ onText, language = 'en', disabled, className }: SttButtonProps) {
  const state = useSignal<State>('idle')
  const error = useSignal<string | null>(null)
  let active: ActiveRecording | null = null

  const cleanup = async () => {
    if (!active) return
    const { ws, ctx, stream, worklet, source } = active
    active = null
    try { source.disconnect() } catch {}
    try { worklet.disconnect() } catch {}
    try { stream.getTracks().forEach(t => t.stop()) } catch {}
    try { await ctx.close() } catch {}
    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      try { ws.close() } catch {}
    }
  }

  const fail = async (msg: string, isUnsupported = false) => {
    error.value = msg
    state.value = 'error'
    if (isUnsupported) unsupported.value = true
    await cleanup()
    setTimeout(() => { if (state.value === 'error') state.value = 'idle' }, 3000)
  }

  const start = async () => {
    if (active || state.value !== 'idle' || disabled || unsupported.value) return
    error.value = null
    state.value = 'recording'

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: TARGET_SAMPLE_RATE,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      })
    } catch {
      return fail(t('stt.mic_denied'))
    }

    const ctx = new AudioContext()
    try {
      await ctx.audioWorklet.addModule(workletUrl())
    } catch {
      stream.getTracks().forEach(t => t.stop())
      await ctx.close()
      return fail(t('stt.audio_unavailable'))
    }
    const source = ctx.createMediaStreamSource(stream)
    const worklet = new AudioWorkletNode(ctx, 'stt-pcm16-downsampler', {
      processorOptions: { targetRate: TARGET_SAMPLE_RATE, sourceRate: ctx.sampleRate },
    })

    const tok = token.value ? `?token=${encodeURIComponent(token.value)}` : ''
    // Relative URL — resolves against ``document.baseURI`` so the
    // ingress prefix is honoured. Modern browsers auto-convert
    // ``http``/``https`` → ``ws``/``wss``.
    const ws = new WebSocket(`api/stt/stream${tok}`)
    ws.binaryType = 'arraybuffer'

    active = { ws, ctx, stream, worklet, source }

    let started = false
    const pending: ArrayBuffer[] = []
    const flushPending = () => {
      while (pending.length) {
        const buf = pending.shift()
        if (buf && ws.readyState === WebSocket.OPEN) ws.send(buf)
      }
    }

    worklet.port.onmessage = (e: MessageEvent) => {
      const buf = e.data as ArrayBuffer
      if (ws.readyState === WebSocket.OPEN && started) {
        ws.send(buf)
      } else {
        pending.push(buf)
      }
    }

    ws.onopen = () => {
      ws.send(JSON.stringify({
        type: 'start', language,
        sample_rate: TARGET_SAMPLE_RATE, channels: 1,
      }))
      started = true
      flushPending()
      source.connect(worklet)
    }

    ws.onmessage = async (e) => {
      if (typeof e.data !== 'string') return
      let msg: { type?: string; text?: string; detail?: string }
      try { msg = JSON.parse(e.data) } catch { return }
      if (msg.type === 'final') {
        if (msg.text) onText(msg.text)
        state.value = 'idle'
        await cleanup()
      } else if (msg.type === 'error') {
        const detail = msg.detail || t('stt.failed')
        const isUnsupported = /not configured|unsupported/i.test(detail)
        await fail(detail, isUnsupported)
      }
    }

    ws.onerror = async () => { await fail(t('stt.connect_failed')) }
    ws.onclose = async () => {
      if (state.value === 'recording' || state.value === 'uploading') {
        await fail(t('stt.closed'))
      }
    }
  }

  const stop = async () => {
    if (!active || state.value !== 'recording') return
    state.value = 'uploading'
    const { ws, source, worklet } = active
    try { source.disconnect(worklet) } catch {}
    if (ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'end' })) } catch {}
    }
  }

  const onPointerDown = (e: Event) => { e.preventDefault(); start() }
  const onPointerUp = (e: Event) => { e.preventDefault(); stop() }
  const onPointerCancel = () => { stop() }

  if (unsupported.value) return null

  // Idle glyph is an inline SVG mic — the previous ``🎙`` emoji
  // rendered as a tofu box on Linux desktops (and any kiosk display
  // running without an emoji font), making the push-to-talk button
  // unrecognisable. The other states stay as small status emoji
  // (red dot / hourglass / warning) since those carry colour cues
  // that are universally legible across emoji-font implementations.
  const idleMic = (
    <svg
      width="22"
      height="22"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <line x1="12" y1="18" x2="12" y2="22" />
      <line x1="8" y1="22" x2="16" y2="22" />
    </svg>
  )
  const label: preact.ComponentChildren =
    state.value === 'recording' ? '🔴' :
    state.value === 'uploading' ? '⏳' :
    state.value === 'error' ? '⚠' : idleMic
  const title =
    state.value === 'error' ? (error.value || t('stt.error')) :
    state.value === 'recording' ? t('stt.release') :
    state.value === 'uploading' ? t('stt.transcribing') :
    t('stt.hold')

  return (
    <button
      type="button"
      class={`sh-stt-btn sh-stt-btn--${state.value} ${className || ''}`}
      title={title}
      aria-label={title}
      aria-pressed={state.value === 'recording'}
      disabled={disabled || state.value === 'uploading'}
      onMouseDown={onPointerDown}
      onMouseUp={onPointerUp}
      onMouseLeave={onPointerCancel}
      onTouchStart={onPointerDown}
      onTouchEnd={onPointerUp}
      onTouchCancel={onPointerCancel}
    >
      <span aria-live="polite">{label}</span>
    </button>
  )
}

// ── AudioWorklet processor ─────────────────────────────────────────────
// ``sttPcm16Worklet.js`` — emitted by Vite as a same-origin asset (the
// ``new URL(…, import.meta.url)`` pattern) so ``script-src 'self'``
// covers it. Resolved against this module's URL, so it follows the
// ingress prefix like every other bundle chunk.
function workletUrl(): string {
  return new URL('./sttPcm16Worklet.js', import.meta.url).href
}
