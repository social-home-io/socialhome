/**
 * The viewer's notification level for one space — "Your notifications"
 * in the space's More panel. The app has ONE bell (the top bar's); the
 * per-space bell that used to sit in the space header is gone (owner
 * decision 6). Sharing your location moved to the Map section.
 *
 * Levels (enforced server-side by
 * NotificationService.on_space_post_created):
 *   - all       — every new post in this space pings the bell
 *   - mentions  — only posts that @mention the caller
 *   - muted     — nothing from this space
 *
 * ``useSpaceNotifLevel`` reads ``GET /api/spaces/{id}/notif-prefs``;
 * ``SpaceNotifLevelPicker`` is a native radio group (arrow keys move
 * between the choices, as in any radio group) that saves on pick with
 * ``PUT …/notif-prefs``.
 */
import { useEffect, useId, useState } from 'preact/hooks'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

export type NotifLevel = 'all' | 'mentions' | 'muted'

const LEVELS: readonly NotifLevel[] = ['all', 'mentions', 'muted']

/** The level's label, in the UI language — looked up at render time. */
export function notifLevelLabel(level: NotifLevel): string {
  switch (level) {
    case 'all': return t('space.notif.level_all')
    case 'mentions': return t('space.notif.level_mentions')
    case 'muted': return t('space.notif.level_muted')
  }
}

function isLevel(v: unknown): v is NotifLevel {
  return typeof v === 'string' && (LEVELS as readonly string[]).includes(v)
}

/** The viewer's level here, ``null`` until (or unless) the server says. */
export function useSpaceNotifLevel(
  spaceId: string, enabled: boolean,
): [NotifLevel | null, (l: NotifLevel) => void] {
  const [level, setLevel] = useState<NotifLevel | null>(null)
  useEffect(() => {
    setLevel(null)
    if (!enabled) return
    let stopped = false
    api.get(`/api/spaces/${spaceId}/notif-prefs`)
      .then((body: { level?: unknown }) => {
        if (!stopped && isLevel(body?.level)) setLevel(body.level)
      })
      .catch(() => { /* no seat / offline — the row shows no level */ })
    return () => { stopped = true }
  }, [spaceId, enabled])
  return [level, setLevel]
}

interface PickerProps {
  spaceId: string
  level: NotifLevel
  /** Called with the saved level. */
  onSaved: (level: NotifLevel) => void
}

export function SpaceNotifLevelPicker({ spaceId, level, onSaved }: PickerProps) {
  const [saving, setSaving] = useState(false)
  const name = useId()

  const choose = async (next: NotifLevel) => {
    if (saving || next === level) return
    setSaving(true)
    try {
      const body = await api.put(
        `/api/spaces/${spaceId}/notif-prefs`, { level: next },
      ) as { level?: unknown }
      const saved = isLevel(body?.level) ? body.level : next
      showToast(
        saved === 'muted'
          ? t('space.notif.muted_toast')
          : saved === 'mentions'
            ? t('space.notif.mentions_toast')
            : t('space.notif.all_toast'),
        'success',
      )
      onSaved(saved)
    } catch (err: unknown) {
      showToast(t('space.notif.save_failed', { error: (err as Error).message }), 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <fieldset class="sh-space-notif-levels" disabled={saving}>
      <legend class="sr-only">{t('space.more.notifications')}</legend>
      {LEVELS.map((opt) => (
        <label key={opt} class="sh-space-notif-levels__option">
          <input
            type="radio"
            name={name}
            value={opt}
            checked={level === opt}
            onChange={() => void choose(opt)}
          />
          <span class="sh-space-notif-levels__text">
            <span class="sh-space-notif-levels__title">{notifLevelLabel(opt)}</span>
            <span class="sh-space-notif-levels__meta">{t(`space.notif.level_${opt}_hint`)}</span>
          </span>
        </label>
      ))}
    </fieldset>
  )
}
