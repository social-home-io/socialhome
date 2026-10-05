/**
 * SpaceNotifPrefsMenu — per-member bell icon that controls the caller's
 * notification level for a single space, and (when the space admin has
 * turned on location sharing) the caller's location opt-in for it.
 *
 * Notification levels (enforced server-side by
 * NotificationService.on_space_post_created):
 *   - all       — every new post in this space pings the bell
 *   - mentions  — only posts that @mention the caller
 *   - muted     — nothing from this space
 *
 * Renders as a small inline menu. The current notif level drives the
 * button icon; the panel offers the three notif options plus, when the
 * space has location sharing on, a Share-my-location toggle that hits
 * the same ``PATCH /api/spaces/{id}/members/me/location-sharing``
 * endpoint as the map chip and the Settings panel. We surface it here
 * because the @-menu is where members already manage their per-space
 * preferences — making the location opt-in discoverable without
 * forcing them to open the map.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

type NotifLevel = 'all' | 'mentions' | 'muted'

interface Props {
  spaceId: string
}

interface PrefsResponse {
  level: NotifLevel
  feature_location: boolean
  location_share_enabled: boolean
}

const LEVEL_ICONS: Record<NotifLevel, string> = {
  all: '🔔',
  mentions: '@',
  muted: '🔕',
}

/** The level's label, in the UI language — looked up at render time. */
function levelLabel(level: NotifLevel): string {
  switch (level) {
    case 'all': return t('space.notif.level_all')
    case 'mentions': return t('space.notif.level_mentions')
    case 'muted': return t('space.notif.level_muted')
  }
}

export function SpaceNotifPrefsMenu({ spaceId }: Props) {
  const [level, setLevel] = useState<NotifLevel>('all')
  const [featureLocation, setFeatureLocation] = useState(false)
  const [shareLocation, setShareLocation] = useState(false)
  const [ready, setReady] = useState(false)
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [savingLocation, setSavingLocation] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let stopped = false
    const load = async () => {
      try {
        const body = await api.get(
          `/api/spaces/${spaceId}/notif-prefs`,
        ) as PrefsResponse
        if (stopped) return
        setLevel(body.level)
        setFeatureLocation(Boolean(body.feature_location))
        setShareLocation(Boolean(body.location_share_enabled))
      } catch {
        // Non-member or network issue — leave defaults + hide menu later.
      } finally {
        if (!stopped) setReady(true)
      }
    }
    void load()
    return () => { stopped = true }
  }, [spaceId])

  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('click', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('click', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!ready) return null

  const choose = async (next: NotifLevel) => {
    if (saving) return
    if (next === level) { setOpen(false); return }
    setSaving(true)
    try {
      const body = await api.put(
        `/api/spaces/${spaceId}/notif-prefs`,
        { level: next },
      ) as { level: NotifLevel }
      setLevel(body.level)
      setOpen(false)
      showToast(
        next === 'muted'
          ? t('space.notif.muted_toast')
          : next === 'mentions'
            ? t('space.notif.mentions_toast')
            : t('space.notif.all_toast'),
        'success',
      )
    } catch (err: unknown) {
      showToast(t('space.notif.save_failed', { error: (err as Error).message }), 'error')
    } finally {
      setSaving(false)
    }
  }

  const toggleLocation = async () => {
    if (savingLocation) return
    const prev = shareLocation
    const next = !prev
    // Optimistic — flip first, revert on failure. Matches the chip on
    // SpaceLocationCard and the row in Settings.
    setShareLocation(next)
    setSavingLocation(true)
    try {
      await api.patch(
        `/api/spaces/${spaceId}/members/me/location-sharing`,
        { enabled: next },
      )
      showToast(
        next
          ? t('space.notif.location_on_toast')
          : t('space.notif.location_off_toast'),
        'success',
      )
    } catch (err: unknown) {
      setShareLocation(prev)
      showToast(t('space.notif.save_failed', { error: (err as Error).message }), 'error')
    } finally {
      setSavingLocation(false)
    }
  }

  return (
    <div class="sh-notif-prefs-menu" ref={wrapRef}>
      <button type="button"
              class="sh-notif-prefs-menu__trigger"
              aria-haspopup="menu"
              aria-expanded={open}
              title={t('space.notif.trigger', { level: levelLabel(level) })}
              onClick={() => setOpen(!open)}>
        <span aria-hidden="true">{LEVEL_ICONS[level]}</span>
        <span class="sr-only">
          {t('space.notif.trigger', { level: levelLabel(level) })}
        </span>
      </button>
      {open && (
        <div class="sh-notif-prefs-menu__panel" role="menu">
          {(['all', 'mentions', 'muted'] as NotifLevel[]).map(opt => (
            <button key={opt}
                    type="button"
                    role="menuitemradio"
                    aria-checked={level === opt}
                    class={level === opt
                      ? 'sh-notif-prefs-menu__item sh-notif-prefs-menu__item--active'
                      : 'sh-notif-prefs-menu__item'}
                    disabled={saving}
                    onClick={() => void choose(opt)}>
              <span aria-hidden="true">{LEVEL_ICONS[opt]}</span>
              <span>{levelLabel(opt)}</span>
              {level === opt && <span aria-hidden="true">✓</span>}
            </button>
          ))}
          {featureLocation && (
            <>
              <div class="sh-notif-prefs-menu__divider" role="separator" />
              <button type="button"
                      role="menuitemcheckbox"
                      aria-checked={shareLocation}
                      class={shareLocation
                        ? 'sh-notif-prefs-menu__item sh-notif-prefs-menu__item--active'
                        : 'sh-notif-prefs-menu__item'}
                      disabled={savingLocation}
                      data-testid="space-location-toggle"
                      onClick={() => void toggleLocation()}>
                <span aria-hidden="true">📍</span>
                <span>{t('space.notif.share_location')}</span>
                {shareLocation && <span aria-hidden="true">✓</span>}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
