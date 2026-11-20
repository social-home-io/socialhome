/**
 * AudioBubble — render a voice-note inside a DM thread.
 *
 * Two regions:
 *   1. A compact player of our own: play / pause, a seek slider, the
 *      time, a playback-speed chip (1× → 1.5× → 2×) and a download
 *      link. The ``<audio>`` element carries no ``controls``.
 *      ``preload="metadata"`` so the duration populates without paying
 *      the full bytes up-front — important on metered mobile data.
 *   2. Transcript line below. When the message is fresh and the
 *      server-side STT hasn't yet patched ``content``, we render a
 *      "Transcribing…" pulse placeholder; when the WS
 *      ``dm.message_updated`` frame lands it patches the message and
 *      the placeholder swaps to the transcript text.
 *
 * Why not the browser's ``<audio controls>``: Chrome's controls put
 * Download and Playback speed behind a ⋮ menu that always opens
 * *upward* from the ⋮ button (it is sized for a video, where the
 * controls sit at the bottom of a tall frame). On a 36-px voice-note
 * player near the top of the thread, the 250-px playback-speed list
 * runs off the top of a phone screen and can't be reached. The menu
 * lives in the browser's own shadow DOM, so no page CSS can move it —
 * owning the controls is the only fix. It also keeps the seek bar
 * usable on a 360-px phone, where Chrome shrank it to a dot.
 *
 * The component is presentational — it doesn't touch the network or
 * subscribe to anything. ``DmThreadPage`` owns the message state and
 * passes ``content`` in as a prop.
 */
import type preact from 'preact'
import { useEffect, useRef, useState } from 'preact/hooks'
import { formatLocale, t } from '@/i18n/i18n'
import { safeHref } from '@/utils/safeHref'

interface AudioBubbleProps {
  src: string
  /** Transcript text. Empty string until the sender's STT (or the
   *  recipient's local fallback) lands. */
  transcript: string
  fileName?: string | null
  /** ``true`` when the message is mid-cross-household-sync — gives the
   *  bubble its brightness-pulse overlay until the full bytes land. */
  pending?: boolean
}

/** The speeds the chip cycles through, WhatsApp-style. */
export const VOICE_SPEEDS = [1, 1.5, 2] as const

/** ``m:ss`` for a voice note (they are capped well under an hour). */
export function formatVoiceTime(seconds: number): string {
  const s = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

function formatSpeed(speed: number): string {
  return `${speed.toLocaleString(formatLocale())}×`
}

export function AudioBubble({
  src,
  transcript,
  fileName,
  pending,
}: AudioBubbleProps): preact.JSX.Element {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [current, setCurrent] = useState(0)
  const [duration, setDuration] = useState(0)
  const [speedIdx, setSpeedIdx] = useState(0)
  /** A recording straight from Chrome's MediaRecorder (WebM) carries
   *  no duration, so ``duration`` reads ``Infinity`` until the file
   *  has been read to the end. Seeking far past the end makes the
   *  browser work it out; this flag swallows that probe's time update. */
  const probing = useRef(false)

  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    probing.current = false
    const readDuration = () => {
      const d = audio.duration
      if (Number.isFinite(d) && d > 0) {
        setDuration(d)
        if (probing.current) {
          probing.current = false
          audio.currentTime = 0
        }
      } else if (d === Infinity && !probing.current) {
        probing.current = true
        audio.currentTime = 1e101
      }
    }
    const onTime = () => {
      if (!probing.current) setCurrent(audio.currentTime)
    }
    const onPlay = () => setPlaying(true)
    const onPause = () => setPlaying(false)
    const onEnded = () => {
      setPlaying(false)
      setCurrent(0)
    }
    audio.addEventListener('loadedmetadata', readDuration)
    audio.addEventListener('durationchange', readDuration)
    audio.addEventListener('timeupdate', onTime)
    audio.addEventListener('play', onPlay)
    audio.addEventListener('pause', onPause)
    audio.addEventListener('ended', onEnded)
    // Metadata that landed before the listeners did (a cached file).
    if (audio.readyState >= HTMLMediaElement.HAVE_METADATA) readDuration()
    return () => {
      audio.removeEventListener('loadedmetadata', readDuration)
      audio.removeEventListener('durationchange', readDuration)
      audio.removeEventListener('timeupdate', onTime)
      audio.removeEventListener('play', onPlay)
      audio.removeEventListener('pause', onPause)
      audio.removeEventListener('ended', onEnded)
    }
  }, [src])

  const toggle = () => {
    const audio = audioRef.current
    if (!audio) return
    if (audio.paused) {
      void audio.play()?.catch(() => { setPlaying(false) })
    } else {
      audio.pause()
    }
  }

  const seek = (e: Event) => {
    const audio = audioRef.current
    if (!audio) return
    const value = Number((e.target as HTMLInputElement).value)
    audio.currentTime = value
    setCurrent(value)
  }

  const cycleSpeed = () => {
    const next = (speedIdx + 1) % VOICE_SPEEDS.length
    setSpeedIdx(next)
    if (audioRef.current) audioRef.current.playbackRate = VOICE_SPEEDS[next]
  }

  const label = fileName ?? t('media.voice_note')
  // A peer-supplied ``javascript:`` URL must never become a link; the
  // download link is left out instead (same gate as the file chip).
  const downloadHref = safeHref(src)
  const speed = formatSpeed(VOICE_SPEEDS[speedIdx])
  return (
    <div class="sh-message-audio">
      <audio
        ref={audioRef}
        src={src}
        preload="metadata"
        aria-label={label}
      />
      <div
        class={
          'sh-message-audio__player'
          + (pending ? ' sh-message-audio__player--pending' : '')
        }
        role="group"
        aria-label={label}
      >
        <button
          type="button"
          class="sh-message-audio__play"
          aria-label={playing ? t('media.voice_pause') : t('media.voice_play')}
          onClick={toggle}
        >
          {playing ? <PauseIcon /> : <PlayIcon />}
        </button>
        <input
          type="range"
          class="sh-message-audio__seek"
          min={0}
          max={duration || 0}
          step="any"
          value={Math.min(current, duration || 0)}
          disabled={!duration}
          aria-label={label}
          aria-valuetext={`${formatVoiceTime(current)} / ${formatVoiceTime(duration)}`}
          onInput={seek}
        />
        <button
          type="button"
          class="sh-message-audio__speed"
          aria-label={t('media.voice_speed', { speed })}
          onClick={cycleSpeed}
        >
          {speed}
        </button>
        {downloadHref !== undefined && (
          <a
            class="sh-message-audio__download"
            href={downloadHref}
            download={fileName ?? ''}
            aria-label={t('media.download')}
            title={t('media.download')}
          >
            <DownloadIcon />
          </a>
        )}
        <span class="sh-message-audio__time">
          {playing || current > 0
            ? `${formatVoiceTime(current)} / ${formatVoiceTime(duration)}`
            : formatVoiceTime(duration)}
        </span>
      </div>
      {transcript ? (
        <div class="sh-message-audio__transcript">
          {transcript}
        </div>
      ) : (
        <div
          class="sh-message-audio__transcript sh-message-audio__transcript--pending"
          aria-live="polite"
        >
          {t('media.transcribing')}
        </div>
      )}
    </div>
  )
}

// SVG, not ▶ / ⤓ glyphs: an emoji font draws ▶ as a coloured tile.
function PlayIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M7 4.5v15a1 1 0 0 0 1.5.87l12-7.5a1 1 0 0 0 0-1.74l-12-7.5A1 1 0 0 0 7 4.5z" />
    </svg>
  )
}

function PauseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <rect x="5" y="4" width="5" height="16" rx="1" />
      <rect x="14" y="4" width="5" height="16" rx="1" />
    </svg>
  )
}

function DownloadIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M12 4v11M7 10l5 5 5-5M5 20h14" />
    </svg>
  )
}
