/**
 * VoiceRecordButton — hold-to-record voice-note capture for DMs.
 *
 * Pointer-down starts a MediaRecorder configured for OGG/Opus mono
 * @ 24 kbps. Pointer-up stops the recorder, assembles the chunks
 * into a Blob, and hands it to ``onCapture(blob)``. Slide upwards
 * past ``LOCK_SLIDE_PX`` while held enters "lock mode" — recording
 * continues after the pointer is released, with explicit Send /
 * Cancel buttons (WhatsApp-Web parity).
 *
 * A hard duration cap (``AUDIO_MAX_DURATION_SECONDS``) auto-stops
 * the recorder so the user can't ship a half-hour blob by accident.
 *
 * This is a sibling component to :class:`SttButton`, which streams
 * live PCM to the STT endpoint and emits text. Voice notes ship
 * audio first and ask the server's STT to fill in the transcript
 * asynchronously, so the two flows share no plumbing apart from
 * ``getUserMedia`` and the unsupported-fallback hook.
 */
import type preact from 'preact'
import { useRef } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

const AUDIO_MAX_DURATION_SECONDS = 300
const LOCK_SLIDE_PX = 60
const AUDIO_BITS_PER_SECOND = 24_000
//: Container preference order — Opus is the preferred codec; the
//: container is whichever the browser's MediaRecorder will give us.
//: Firefox supports OGG/Opus natively. Chromium-based browsers
//: (Chrome / Edge) only emit WebM/Opus. Safari has no Opus in
//: MediaRecorder; it emits MP4/AAC. Server-side PyAV decodes all
//: three losslessly to PCM before STT.
const MIME_CANDIDATES: readonly string[] = [
  'audio/ogg; codecs=opus',
  'audio/webm; codecs=opus',
  'audio/mp4; codecs=mp4a.40.2',
  'audio/mp4',
]

type State = 'idle' | 'recording' | 'locked' | 'unsupported'

interface VoiceRecordButtonProps {
  onCapture: (blob: Blob) => void
  disabled?: boolean
  className?: string
}

interface ActiveCapture {
  recorder: MediaRecorder
  stream: MediaStream
  chunks: Blob[]
  startedAt: number
  capTimer: ReturnType<typeof setTimeout>
  pointerStartY: number
  lockedSent: boolean
}

/** First MediaRecorder MIME the browser supports, or ``null``. Picks
 *  OGG/Opus when the browser has it (Firefox); otherwise WebM/Opus
 *  (Chromium-based). Safari currently exposes neither — recordings
 *  there sit on the unsupported branch. */
function pickRecorderMime(): string | null {
  if (typeof MediaRecorder === 'undefined') return null
  for (const candidate of MIME_CANDIDATES) {
    try {
      if (MediaRecorder.isTypeSupported(candidate)) return candidate
    } catch {
      /* feature-detect failed — try the next one */
    }
  }
  return null
}

const recorderSupported = (): boolean => pickRecorderMime() !== null

export function VoiceRecordButton({
  onCapture,
  disabled,
  className,
}: VoiceRecordButtonProps): preact.JSX.Element | null {
  const state = useSignal<State>(recorderSupported() ? 'idle' : 'unsupported')
  const error = useSignal<string | null>(null)
  // Seconds elapsed while recording — drives the inline timer label.
  const elapsedSec = useSignal(0)
  // ``active`` and the elapsed-timer handle live across renders — a
  // ``let`` inside the function body would be re-initialised every
  // time the signals trigger a re-render and we'd lose the recorder
  // reference between pointerdown and pointerup.
  const activeRef = useRef<ActiveCapture | null>(null)
  const elapsedTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const teardownStream = async () => {
    const active = activeRef.current
    if (!active) return
    const { recorder, stream, capTimer } = active
    activeRef.current = null
    if (elapsedTimerRef.current) {
      clearInterval(elapsedTimerRef.current)
      elapsedTimerRef.current = null
    }
    clearTimeout(capTimer)
    try {
      if (recorder.state !== 'inactive') recorder.stop()
    } catch {
      /* already stopped */
    }
    try {
      stream.getTracks().forEach(tr => tr.stop())
    } catch {
      /* tracks already gone */
    }
  }

  const finalise = (sendIt: boolean) => {
    const active = activeRef.current
    if (!active) return
    const { recorder, chunks } = active
    active.lockedSent = sendIt
    // ``onstop`` runs after the final ``ondataavailable`` event so we
    // capture the blob inside that callback. Set up the callback once
    // and then ask the recorder to flush.
    recorder.onstop = () => {
      if (sendIt && chunks.length > 0) {
        // Carry the recorder's actual MIME on the final blob so the
        // upload path stamps it on the message + selects the right
        // backend processor branch. Fall back to the first chunk's
        // type if MediaRecorder didn't set ``mimeType`` (rare).
        const blob = new Blob(chunks, {
          type: recorder.mimeType || chunks[0].type || 'audio/webm',
        })
        onCapture(blob)
      }
      void teardownStream().then(() => {
        state.value = 'idle'
        elapsedSec.value = 0
      })
    }
    try {
      if (recorder.state !== 'inactive') recorder.stop()
    } catch {
      void teardownStream()
      state.value = 'idle'
      elapsedSec.value = 0
    }
  }

  const start = async (pointerY: number) => {
    if (state.value !== 'idle' || disabled) return
    error.value = null

    // ``getUserMedia`` only exists on secure contexts: HTTPS, plus
    // ``localhost`` as a developer convenience. The Android browser
    // is strict about this and silently leaves ``navigator.mediaDevices``
    // undefined on plain-HTTP origins — checking this up-front gives the
    // user a real explanation instead of "the mic button does nothing".
    if (!navigator.mediaDevices?.getUserMedia) {
      const msg = window.isSecureContext
        ? t('voice.no_mic_support')
        : t('voice.needs_https')
      error.value = msg
      showToast(msg, 'error')
      return
    }

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: 48000,
          echoCancellation: true,
          noiseSuppression: true,
        },
      })
    } catch (err: unknown) {
      // ``DOMException.name`` distinguishes "user said no" (``NotAllowedError``)
      // from "no microphone exists" (``NotFoundError``) — both warrant a
      // visible toast so the press isn't a silent no-op.
      const name = (err as DOMException)?.name ?? ''
      const msg =
        name === 'NotAllowedError'
          ? t('voice.mic_denied')
          : name === 'NotFoundError'
            ? t('voice.no_mic')
            : t('voice.mic_failed')
      error.value = msg
      showToast(msg, 'error')
      return
    }

    const pickedMime = pickRecorderMime()
    if (pickedMime === null) {
      stream.getTracks().forEach(tr => tr.stop())
      const msg = t('voice.recording_unavailable')
      error.value = msg
      showToast(msg, 'error')
      state.value = 'unsupported'
      return
    }
    let recorder: MediaRecorder
    try {
      recorder = new MediaRecorder(stream, {
        mimeType: pickedMime,
        audioBitsPerSecond: AUDIO_BITS_PER_SECOND,
      })
    } catch {
      stream.getTracks().forEach(tr => tr.stop())
      const msg = t('voice.recording_unavailable')
      error.value = msg
      showToast(msg, 'error')
      state.value = 'unsupported'
      return
    }

    const chunks: Blob[] = []
    recorder.ondataavailable = (e: BlobEvent) => {
      if (e.data && e.data.size > 0) chunks.push(e.data)
    }

    const capTimer = setTimeout(() => {
      // 5-min hard cap. Force-send whatever we have rather than
      // dropping — the user clearly meant for *something* to ship.
      error.value = t('voice.capped')
      finalise(true)
    }, AUDIO_MAX_DURATION_SECONDS * 1000)

    activeRef.current = {
      recorder,
      stream,
      chunks,
      startedAt: Date.now(),
      capTimer,
      pointerStartY: pointerY,
      lockedSent: false,
    }
    recorder.start()
    state.value = 'recording'
    elapsedSec.value = 0
    elapsedTimerRef.current = setInterval(() => {
      const a = activeRef.current
      if (!a) return
      elapsedSec.value = Math.round((Date.now() - a.startedAt) / 1000)
    }, 200)
  }

  const onPointerDown = (e: PointerEvent) => {
    e.preventDefault()
    // Capture the pointer so subsequent move / up events fire on this
    // button even if the finger drifts off it — without this, an
    // Android user touching the mic and sliding even a hair lands
    // pointerup on a different element and the recording is silently
    // orphaned. ``setPointerCapture`` is best-effort: jsdom + older
    // Safari throw on the call.
    const target = e.currentTarget as Element | null
    try {
      target?.setPointerCapture?.(e.pointerId)
    } catch {
      /* unsupported — gesture still works as long as the finger
         stays on the button */
    }
    void start(e.clientY)
  }
  const onPointerMove = (e: PointerEvent) => {
    const active = activeRef.current
    if (!active || state.value !== 'recording') return
    if (active.pointerStartY - e.clientY >= LOCK_SLIDE_PX) {
      // User swiped up far enough to lock — stay recording until
      // they hit Send / Cancel.
      state.value = 'locked'
    }
  }
  const onPointerUp = (e: PointerEvent) => {
    e.preventDefault()
    if (!activeRef.current) return
    if (state.value === 'locked') return // ignore the release while locked
    finalise(true)
  }
  const onPointerCancel = () => {
    if (!activeRef.current) return
    if (state.value === 'locked') return
    finalise(false)
  }

  // The unsupported case stays mounted as a disabled button so the
  // composer layout doesn't reflow when the recorder is missing — a
  // disabled mic with a tooltip is friendlier than a vanishing
  // affordance.
  if (state.value === 'unsupported') {
    return (
      <button
        type="button"
        class={`sh-voice-btn sh-voice-btn--disabled ${className || ''}`}
        title={t('voice.unsupported_title')}
        aria-label={t('voice.unsupported_aria')}
        disabled
      >
        <MicIcon />
      </button>
    )
  }

  const elapsedLabel = formatElapsed(elapsedSec.value)
  const isRecording = state.value === 'recording'
  const isLocked = state.value === 'locked'

  if (isLocked) {
    return (
      <div class="sh-voice-locked" role="group" aria-label={t('voice.locked_aria')}>
        <span class="sh-voice-locked__indicator" aria-hidden="true">●</span>
        <span class="sh-voice-locked__elapsed">{elapsedLabel}</span>
        <button
          type="button"
          class="sh-voice-locked__btn sh-voice-locked__btn--cancel"
          onClick={() => finalise(false)}
        >
          {t('common.cancel')}
        </button>
        <button
          type="button"
          class="sh-voice-locked__btn sh-voice-locked__btn--send"
          onClick={() => finalise(true)}
        >
          {t('dms.send')}
        </button>
      </div>
    )
  }

  return (
    <button
      type="button"
      class={`sh-voice-btn ${isRecording ? 'sh-voice-btn--recording' : ''} ${className || ''}`}
      title={
        isRecording
          ? t('voice.release_hint')
          : t('voice.hold_to_record')
      }
      aria-label={isRecording ? t('voice.recording_aria') : t('voice.hold_to_record')}
      aria-pressed={isRecording}
      disabled={disabled}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onPointerLeave={onPointerCancel}
    >
      {isRecording ? (
        <span class="sh-voice-btn__elapsed">
          <span class="sh-voice-btn__pulse" aria-hidden="true">●</span>
          {elapsedLabel}
        </span>
      ) : (
        <MicIcon />
      )}
    </button>
  )
}

function MicIcon() {
  return (
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
}

function formatElapsed(totalSeconds: number): string {
  const mins = Math.floor(totalSeconds / 60)
  const secs = totalSeconds % 60
  return `${mins}:${secs.toString().padStart(2, '0')}`
}
