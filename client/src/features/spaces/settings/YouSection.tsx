/**
 * A member's own settings in a space they don't run:
 *
 *   - ``YouSection`` — "You in this space": the name and picture shown
 *     here (``SpaceProfileDialog``), and — when the space has a map —
 *     whether you share your location (a link to the Map, where that
 *     switch lives), plus where a global space is listed.
 *   - ``LeaveSection`` / ``UnfollowSection`` — a collapsed row at the end,
 *     so leaving is one deliberate step away.
 */
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { confirmDialog } from '@/components/confirm'
import {
  SpaceGfsServersReadOnly, hasGfsServersToShow,
} from '@/components/SpaceGfsListing'
import { SpaceProfileDialog, openSpaceProfileDialog } from '@/components/SpaceProfileDialog'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import type { Space } from '@/types'
import { LinkRow } from './SettingsRows'

export interface MySeat {
  display_name?: string | null
  space_display_name?: string | null
}

export function YouSection({ space, seat, locationAllowed, sharing }: {
  space: Space
  seat: MySeat | null
  /** The space has a map and the viewer may share on it. */
  locationAllowed: boolean
  sharing: boolean
}) {
  const own = seat?.space_display_name
  const name = own || seat?.display_name || currentUser.value?.display_name || ''
  return (
    <div class="sh-ss-card">
      <div class="sh-ss-links">
        <LinkRow
          title={t('space.settings_page.you.name')}
          meta={own
            ? t('space.settings_page.you.name_own', { name })
            : t('space.settings_page.you.name_household', { name })}
          onClick={() => openSpaceProfileDialog(space.id)}
          testId="settings-you-profile"
        />
        {locationAllowed && (
          <LinkRow
            href={addBase(`/spaces/${space.id}/map`)}
            title={t('space.settings_page.you.location')}
            meta={sharing ? t('space.settings_page.you.location_on') : t('space.settings_page.you.location_off')}
            testId="settings-you-location"
          />
        )}
      </div>
      {hasGfsServersToShow(space) && (
        <div data-testid="member-gfs-servers">
          <h3 class="sh-ss-subheading">{t('space.federation')}</h3>
          <SpaceGfsServersReadOnly space={space} />
        </div>
      )}
      <SpaceProfileDialog />
    </div>
  )
}

export function LeaveSection({ space }: { space: Space }) {
  const { route } = useLocation()
  const leave = async () => {
    const ok = await confirmDialog(t('space.leave.confirm', { name: space.name }), {
      destructive: true, confirmLabel: t('space.leave.confirm_action'),
    })
    const me = currentUser.value?.user_id
    if (!ok || !me) return
    try {
      await api.delete(`/api/spaces/${space.id}/members/${me}`)
      showToast(t('space.leave.left', { name: space.name }), 'info')
      route('/spaces', true)
    } catch (err) {
      showToast(t('space.leave.failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }
  return (
    <details class="sh-ss-danger" data-testid="settings-leave">
      <summary class="sh-ss-danger__summary">
        <span class="sh-ss-danger__title">{t('space.settings_page.leave.summary')}</span>
        <span class="sh-ss-danger__meta">{t('space.settings_page.leave.meta')}</span>
      </summary>
      <div class="sh-ss-danger__body">
        <Button variant="danger" onClick={() => void leave()}>{t('space.leave.button')}</Button>
      </div>
    </details>
  )
}

export function UnfollowSection({ space }: { space: Space }) {
  const { route } = useLocation()
  const unfollow = async () => {
    try {
      await api.delete(`/api/spaces/${space.id}/subscribe`)
      showToast(t('space.subscriber.unsubscribed'), 'info')
      route('/spaces', true)
    } catch (err) {
      showToast((err as Error).message, 'error')
    }
  }
  return (
    <details class="sh-ss-danger" data-testid="settings-unfollow">
      <summary class="sh-ss-danger__summary">
        <span class="sh-ss-danger__title">{t('space.settings_page.unfollow.summary')}</span>
        <span class="sh-ss-danger__meta">{t('space.settings_page.unfollow.meta')}</span>
      </summary>
      <div class="sh-ss-danger__body">
        <Button variant="secondary" onClick={() => void unfollow()}>
          {t('space.subscriber.unsubscribe')}
        </Button>
      </div>
    </details>
  )
}
