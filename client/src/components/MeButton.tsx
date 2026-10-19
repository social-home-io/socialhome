/**
 * MeButton — the "You" menu at the right end of the top bar.
 *
 * Status (emoji + text + expiry) is something you *do*, not long-term
 * profile identity, so it lives here — one tap from every page, at every
 * width, in every platform mode (haos included: HA's own sidebar shows
 * who is signed in to Home Assistant, not which household member).
 *
 * The trigger is the caller's avatar with the live status emoji as a
 * small badge. The menu (``OverflowMenu``) leads with a non-focusable
 * header — name, @handle, the status and when it clears — then: set /
 * edit status (opens ``StatusEditorHost``), clear status (only while
 * one is set) and Profile & settings.
 *
 * Liveness: it only reads ``currentUser``, which ``store/myStatus`` keeps
 * in step with our own ``user.status_changed`` frames (another device,
 * the server's expiry sweep). A timer re-renders at the deadline so the
 * badge leaves on time even before the sweep runs.
 */
import { useEffect, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import type { UserStatus } from '@/types'
import { Avatar } from './Avatar'
import { OverflowMenu, type MenuItem } from './OverflowMenu'
import { clearMyStatus, formatClearsAt, liveStatus, openStatusEditor } from './StatusEditor'

/** ``setTimeout`` caps at 2^31-1 ms (~24.8 days); re-arm past that. */
const MAX_TIMER_MS = 2_147_483_647

/** "🌴 On leave" — the status as one line, or ``null``. */
function statusText(s: UserStatus | null): string | null {
  if (!s) return null
  return [s.emoji, s.text].filter(Boolean).join(' ') || null
}

export function MeButton() {
  const loc = useLocation()
  const [tick, setTick] = useState(0)
  const user = currentUser.value
  const status = liveStatus(user?.status)
  const expiresAt = status?.expires_at ?? null

  // Re-render at the deadline so ``liveStatus`` drops the badge on time.
  // A deadline past the setTimeout cap fires early; ``tick`` re-arms it.
  useEffect(() => {
    if (!expiresAt) return
    const ms = Date.parse(expiresAt) - Date.now()
    if (!(ms > 0)) return
    const id = setTimeout(() => setTick(n => n + 1), Math.min(ms + 50, MAX_TIMER_MS))
    return () => clearTimeout(id)
  }, [expiresAt, tick])

  if (!user) return null

  const name = user.display_name || user.username
  const handle = user.handle ?? user.username
  const line = statusText(status)
  const label = line
    ? t('me.menu_aria_status', { name, status: line })
    : t('me.menu_aria', { name })

  const items: MenuItem[] = [
    {
      key: 'status',
      label: status ? t('me.edit_status') : t('me.set_status'),
      onSelect: openStatusEditor,
    },
    ...(status ? [{
      key: 'clear',
      label: t('status.clear_status'),
      onSelect: () => { void clearMyStatus() },
    }] : []),
    {
      key: 'settings',
      label: t('me.profile_settings'),
      onSelect: () => loc.route('/settings'),
    },
  ]

  const header = (
    <div class="sh-me-menu__who">
      <span class="sh-me-menu__name">{name}</span>
      {handle && <span class="sh-me-menu__handle">@{handle}</span>}
      <span class={line ? 'sh-me-menu__status' : 'sh-me-menu__status sh-muted'}>
        {line ?? t('me.no_status')}
        {line && expiresAt && (
          <span class="sh-me-menu__until">
            {' · '}{t('status.until', { time: formatClearsAt(expiresAt) })}
          </span>
        )}
      </span>
    </div>
  )

  return (
    <OverflowMenu
      label={label}
      title={name}
      items={items}
      header={header}
      bareTrigger
      floating
      menuWidth={240}
      triggerClass="sh-me-btn"
      wrapClass="sh-me"
      menuClass="sh-me-menu"
    >
      <Avatar src={user.picture_url} name={name} size={40} />
      {/* A text-only status still gets a cue: a small dot. */}
      {status && (
        <span
          class={status.emoji ? 'sh-me-btn__badge' : 'sh-me-btn__badge sh-me-btn__badge--dot'}
          aria-hidden="true"
          data-testid="me-status-badge"
        >
          {status.emoji}
        </span>
      )}
    </OverflowMenu>
  )
}
