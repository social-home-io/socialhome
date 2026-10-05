/**
 * AudioBubble — render a voice-note inside a DM thread.
 *
 * Two regions:
 *   1. Inline ``<audio controls>`` for playback. ``preload="metadata"``
 *      so the duration / scrubber populates without paying the full
 *      bytes up-front — important on metered mobile data.
 *   2. Transcript line below. When the message is fresh and the
 *      server-side STT hasn't yet patched ``content``, we render a
 *      "Transcribing…" pulse placeholder; when the WS
 *      ``dm.message_updated`` frame lands it patches the message and
 *      the placeholder swaps to the transcript text.
 *
 * The component is presentational — it doesn't touch the network or
 * subscribe to anything. ``DmThreadPage`` owns the message state and
 * passes ``content`` in as a prop.
 */
import type preact from 'preact'
import { t } from '@/i18n/i18n'

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

export function AudioBubble({
  src,
  transcript,
  fileName,
  pending,
}: AudioBubbleProps): preact.JSX.Element {
  return (
    <div class="sh-message-audio">
      <audio
        class={
          'sh-message-audio__player'
          + (pending ? ' sh-message-audio__player--pending' : '')
        }
        src={src}
        controls
        preload="metadata"
        aria-label={fileName ?? t('media.voice_note')}
      />
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
