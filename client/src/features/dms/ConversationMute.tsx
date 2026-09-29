/**
 * ConversationMute — mute a DM / group for yourself (§23.47).
 *
 * Two surfaces on the same ``PUT|DELETE /api/conversations/{id}/mute``:
 *
 *   • :func:`MuteButton` — the thread header's bell. Unmuted it opens a
 *     small menu (1 hour / 8 hours / 1 week / until I turn it back on);
 *     muted it shows the bell-slash and one click unmutes.
 *   • :func:`MuteSection` — the Group info dialog's "Notifications" block:
 *     the current state, the same four lengths, or Unmute.
 *
 * A mute is the viewer's own and local: messages still arrive and count
 * unread, they just raise no bell row or push. The parent owns
 * ``mutedUntil`` (from ``GET /api/conversations``) and gets the new value
 * through ``onChange``.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { setConversationMute } from '@/store/dms'
import {
  MUTE_DURATIONS,
  isMuteActive,
  muteDurationLabel,
  mutedLabel,
  type MuteDuration,
} from '@/utils/mute'

interface Props {
  convId: string
  mutedUntil: string | null
  onChange: (mutedUntil: string | null) => void
}

async function apply(
  convId: string,
  duration: MuteDuration | null,
  onChange: (until: string | null) => void,
): Promise<boolean> {
  try {
    const until = await setConversationMute(convId, duration)
    onChange(until)
    showToast(t(duration === null ? 'dms.mute.toast_unmuted' : 'dms.mute.toast_muted'), 'success')
    return true
  } catch {
    showToast(t('dms.mute.error'), 'error')
    return false
  }
}

export function MuteButton({ convId, mutedUntil, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const firstItemRef = useRef<HTMLButtonElement | null>(null)
  const muted = isMuteActive(mutedUntil)

  useEffect(() => {
    if (!open) return
    firstItemRef.current?.focus()
    const onDoc = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const run = async (duration: MuteDuration | null) => {
    if (saving) return
    setSaving(true)
    if (await apply(convId, duration, onChange)) setOpen(false)
    setSaving(false)
  }

  if (muted) {
    const hint = t('dms.mute.unmute_hint', { state: mutedLabel(mutedUntil as string) })
    return (
      <button
        type="button"
        class="sh-icon-btn sh-thread-mute-btn sh-thread-mute-btn--muted"
        title={hint}
        aria-label={hint}
        disabled={saving}
        onClick={() => { void run(null) }}
      >
        <span aria-hidden="true">🔕</span>
      </button>
    )
  }

  return (
    <div class="sh-thread-mute" ref={wrapRef}>
      <button
        type="button"
        class="sh-icon-btn sh-thread-mute-btn"
        title={t('dms.mute.menu_title')}
        aria-label={t('dms.mute.menu_title')}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span aria-hidden="true">🔔</span>
      </button>
      {open && (
        <div class="sh-thread-mute__panel" role="menu" aria-label={t('dms.mute.menu_title')}>
          <div class="sh-thread-mute__title" aria-hidden="true">{t('dms.mute.menu_title')}</div>
          {MUTE_DURATIONS.map((d, i) => (
            <button
              key={d}
              ref={i === 0 ? firstItemRef : undefined}
              type="button"
              role="menuitem"
              class="sh-thread-mute__item"
              disabled={saving}
              onClick={() => { void run(d) }}
            >
              {muteDurationLabel(d)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export function MuteSection({ convId, mutedUntil, onChange }: Props) {
  const [saving, setSaving] = useState(false)
  const muted = isMuteActive(mutedUntil)
  const run = async (duration: MuteDuration | null) => {
    if (saving) return
    setSaving(true)
    await apply(convId, duration, onChange)
    setSaving(false)
  }
  return (
    <section class="sh-groupinfo-mute" aria-labelledby={`sh-mute-${convId}`}>
      <h3 class="sh-groupinfo-heading" id={`sh-mute-${convId}`}>
        {t('dms.mute.section')}
      </h3>
      {muted ? (
        <div class="sh-groupinfo-mute__row">
          <span class="sh-groupinfo-mute__state">
            <span aria-hidden="true">🔕 </span>{mutedLabel(mutedUntil as string)}
          </span>
          <Button
            variant="secondary"
            disabled={saving}
            onClick={() => { void run(null) }}
          >
            {t('dms.mute.unmute')}
          </Button>
        </div>
      ) : (
        <>
          <p class="sh-muted sh-groupinfo-mute__state">{t('dms.mute.on')}</p>
          <div class="sh-groupinfo-mute__options" role="group" aria-label={t('dms.mute.menu_title')}>
            {MUTE_DURATIONS.map(d => (
              <button
                key={d}
                type="button"
                class="sh-chip"
                disabled={saving}
                onClick={() => { void run(d) }}
              >
                {muteDurationLabel(d)}
              </button>
            ))}
          </div>
        </>
      )}
    </section>
  )
}
