/**
 * rowParts — the pieces a highlight row is made of, shared by Today
 * (:file:`HighlightsInboxTab`) and the Archive
 * (:file:`HighlightArchiveTab`): a frame still, the audience chip and
 * "3 frames" / "2 videos".
 */
import { useState } from 'preact/hooks'
import type { Highlight, HighlightFrame } from '@/types'
import { t, isOne } from '@/i18n/i18n'
import { audienceChip } from './today'
import { CHIP_ICON, SVG } from './icons'

const PLAY = (<svg {...SVG} width={18} height={18}><path d="M7 4l13 8-13 8z" fill="currentColor" /></svg>)

/** The audience chip — a short label on its own wrap line. Viewers get
 *  the coarse label, never who else was picked. */
export function AudienceChip({ highlight, mine }: { highlight: Highlight; mine: boolean }) {
  const chip = audienceChip(highlight, mine)
  return (
    <span class="sh-hl-chip">
      {CHIP_ICON[chip.icon]}
      <span class="sr-only">{t('highlight.chip.aria', { who: chip.text })}</span>
      <span aria-hidden="true">{chip.text}</span>
    </span>
  )
}

/** One frame as a still: the picture, a video's poster with a play
 *  glyph, or a neutral tile when there is no poster (yet). */
export function FrameThumb({ frame }: { frame: HighlightFrame }) {
  const [broken, setBroken] = useState(false)
  const isVideo = frame.frame_type === 'video'
  const src = isVideo ? frame.thumbnail_url : frame.media_url
  return (
    <span class={isVideo ? 'sh-hl-thumb sh-hl-thumb--video' : 'sh-hl-thumb'}>
      {src && !broken && (
        <img src={src} alt="" loading="lazy" onError={() => setBroken(true)} />
      )}
      {isVideo && <span class="sh-hl-thumb__play">{PLAY}</span>}
    </span>
  )
}

/** "3 frames", or "2 videos" when every frame is a video. */
export function frameCount(frames: readonly HighlightFrame[]): string {
  const n = frames.length
  const allVideo = n > 0 && frames.every(f => f.frame_type === 'video')
  const key = allVideo ? 'highlight.today.videos' : 'highlight.frames'
  return t(isOne(n) ? `${key}_one` : key, { n: String(n) })
}
