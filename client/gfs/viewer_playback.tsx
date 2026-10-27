/* Playback controls shared by the public viewers (highlight + moment).
 *
 * A bar of round buttons under the stage — previous, pause / play, next,
 * mute / unmute — each at least 44 px, named for screen readers, plus the
 * keyboard: Space or K pauses, ← / → step (swapped on a right-to-left
 * page, where "next" is on the left), M mutes. A button that has nothing
 * to do is left out rather than shown disabled.
 *
 * Video starts muted: browsers only autoplay muted media, and an unmuted
 * ``autoplay`` that the browser refuses leaves a still frame with a timer
 * running past it. Unmute is one press away.
 */
import { useEffect, useRef } from 'preact/hooks'
import { t } from './viewer_i18n'

export interface PlaybackActions {
  togglePause: () => void
  toggleMute?: () => void
  prev?: () => void
  next?: () => void
}

/** Whether the visitor asked for less motion. No ``matchMedia`` (old
 *  browsers, tests): assume motion is fine. */
export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return !!window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** Whether the page reads right to left. */
export function isRtl(): boolean {
  const html = document.documentElement
  if (html.dir) return html.dir.toLowerCase() === 'rtl'
  return getComputedStyle(html).direction === 'rtl'
}

// Space on a focused control is that control's own click.
const OWN_SPACE = 'button, a[href], input, textarea, select, summary, [contenteditable]'

/** The keyboard shortcuts, on the whole page, while mounted. */
export function usePlaybackKeys(actions: PlaybackActions): void {
  // The latest actions, without re-registering the listener per render.
  const latest = useRef(actions)
  latest.current = actions
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return
      const a = latest.current
      let run: (() => void) | undefined
      switch (e.key) {
        case ' ':
        case 'Spacebar': {
          const target = e.target instanceof Element ? e.target : null
          if (target?.closest(OWN_SPACE)) return
          run = a.togglePause
          break
        }
        case 'k':
        case 'K':
          run = a.togglePause
          break
        case 'm':
        case 'M':
          run = a.toggleMute
          break
        case 'ArrowLeft':
          run = isRtl() ? a.next : a.prev
          break
        case 'ArrowRight':
          run = isRtl() ? a.prev : a.next
          break
      }
      if (!run) return
      e.preventDefault()
      run()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])
}

/* 24 px glyphs, drawn in currentColor. No inline styles: the pinned page's
 * CSP admits only its hashed <style>. Previous / next mirror on a
 * right-to-left page (the frames run that way too); play / pause and the
 * speaker don't, as media controls never do. */
function Icon({ d, flip }: { d: string; flip?: boolean }) {
  return (
    <svg
      class={flip ? 'viewer-flip' : undefined}
      viewBox="0 0 24 24"
      width="22"
      height="22"
      aria-hidden="true"
      focusable="false"
    >
      <path d={d} fill="currentColor" />
    </svg>
  )
}

const ICON = {
  prev: 'M6 5h2v14H6zM20 5v14L9.5 12z',
  next: 'M16 5h2v14h-2zM4 5v14l10.5-7z',
  pause: 'M7 5h3.5v14H7zM13.5 5H17v14h-3.5z',
  play: 'M8 5v14l11-7z',
  // A speaker; with a cross (muted) or with waves (sound on).
  muted: 'M4 9h4l5-4v14l-5-4H4zM16.3 9.7l1.4-1.4L20 10.6l2.3-2.3 1.4 1.4-2.3 2.3 2.3 2.3-1.4 1.4-2.3-2.3-2.3 2.3-1.4-1.4 2.3-2.3z',
  sound: 'M4 9h4l5-4v14l-5-4H4zM15.5 8.5a5 5 0 0 1 0 7l-1.4-1.4a3 3 0 0 0 0-4.2zM18.3 5.7a9 9 0 0 1 0 12.6l-1.4-1.4a7 7 0 0 0 0-9.8z',
}

/** The control bar. ``muted: null`` leaves out the mute button (nothing in
 *  this highlight has sound); a missing ``onPrev`` / ``onNext`` leaves out
 *  that button (a moment is one item). */
export function PlaybackControls({
  paused,
  muted,
  onTogglePause,
  onToggleMute,
  onPrev,
  onNext,
}: {
  paused: boolean
  muted: boolean | null
  onTogglePause: () => void
  onToggleMute?: () => void
  onPrev?: () => void
  onNext?: () => void
}) {
  return (
    <div class="viewer-controls" role="group" aria-label={t('ctl.group')}>
      {onPrev && (
        <button type="button" class="viewer-ctl" aria-label={t('ctl.prev')} onClick={onPrev}>
          <Icon d={ICON.prev} flip />
        </button>
      )}
      <button
        type="button"
        class="viewer-ctl viewer-ctl-main"
        aria-label={paused ? t('ctl.play') : t('ctl.pause')}
        aria-keyshortcuts="Space K"
        onClick={onTogglePause}
      >
        <Icon d={paused ? ICON.play : ICON.pause} />
      </button>
      {onNext && (
        <button type="button" class="viewer-ctl" aria-label={t('ctl.next')} onClick={onNext}>
          <Icon d={ICON.next} flip />
        </button>
      )}
      {muted !== null && onToggleMute && (
        <button
          type="button"
          class="viewer-ctl"
          aria-label={muted ? t('ctl.unmute') : t('ctl.mute')}
          aria-keyshortcuts="M"
          onClick={onToggleMute}
        >
          <Icon d={muted ? ICON.muted : ICON.sound} />
        </button>
      )}
    </div>
  )
}
