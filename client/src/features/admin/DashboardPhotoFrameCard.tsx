/**
 * Admin › Dashboard › Photo screensaver (gallery decision D10).
 *
 * Off by default. When on, the wall tablets and wall panels show photos
 * from the albums picked here after a while without a touch; any tap
 * brings the board back. Only household albums everyone at home sees
 * can be picked — a private album never shows on the wall, and neither
 * does a space album. Night mode and the privacy lock hide it.
 *
 * Saves as it changes through the tab's optimistic ``save``.
 */
import { CheckboxCardGroup } from '@/components/CheckboxCardGroup'
import { isOne, t } from '@/i18n/i18n'
import { albumDisplayName } from '@/features/gallery/albumName'
import { SwitchRow } from './DashboardSwitchRow'
import type { DashboardState, SaveFn } from './dashboardTypes'

/** ``PHOTO_FRAME_MINUTES_CHOICES`` on the server. */
const FRAME_MINUTES = [1, 2, 5, 10, 15, 30] as const

export function DashboardPhotoFrameCard({ state, save }: { state: DashboardState, save: SaveFn }) {
  const on = state.photo_frame
  const picked = new Set(state.photo_frame_albums)
  const choices = state.photo_frame_album_choices
  const toggle = (id: string) => {
    // Keep the stored order; drop ids no longer offered (made private,
    // deleted) whenever the admin changes the pick.
    const offered = new Set(choices.map((a) => a.id))
    const next = picked.has(id)
      ? state.photo_frame_albums.filter((a) => a !== id)
      : [...state.photo_frame_albums, id]
    void save({ photo_frame_albums: next.filter((a) => offered.has(a)) })
  }
  const pickedShown = choices.filter((a) => picked.has(a.id)).length

  return (
    <section class="sh-admin-section" aria-labelledby="sh-dash-frame">
      <h3 id="sh-dash-frame">{t('admin.dashboard.frame.heading')}</h3>
      <p class="sh-muted">{t('admin.dashboard.frame.intro')}</p>
      <SwitchRow id="sh-dash-frame-on" label={t('admin.dashboard.frame.switch')}
        hint={t('admin.dashboard.frame.switch_hint')} checked={on}
        onChange={(v) => void save({ photo_frame: v })} />
      <div class={`sh-dashboard-row sh-dashboard-row--select${on ? '' : ' sh-dashboard-row--disabled'}`}>
        <div class="sh-dashboard-row__label">
          <label for="sh-dash-frame-after">{t('admin.dashboard.frame.after')}</label>
        </div>
        <select id="sh-dash-frame-after" class="sh-dashboard-row__control"
          value={String(state.photo_frame_minutes)} disabled={!on}
          onChange={(e) => void save({ photo_frame_minutes: Number((e.target as HTMLSelectElement).value) })}>
          {FRAME_MINUTES.map((n) => (
            <option key={n} value={String(n)}>{t('admin.dashboard.display.idle_minutes', { n: String(n) })}</option>
          ))}
        </select>
      </div>
      {choices.length === 0 ? (
        <p class="sh-muted sh-dashboard-frame__empty">{t('admin.dashboard.frame.no_albums')}</p>
      ) : (
        <CheckboxCardGroup
          legend={t('admin.dashboard.frame.albums')}
          disabled={!on}
          onToggle={toggle}
          options={choices.map((a) => ({
            value: a.id,
            icon: a.is_system ? '📸' : '🖼️',
            title: albumDisplayName(a),
            subtitle: t(isOne(a.photo_count) ? 'admin.dashboard.frame.photo_count_one' : 'admin.dashboard.frame.photo_count',
              { n: String(a.photo_count) }),
            checked: picked.has(a.id),
          }))}
        />
      )}
      <p class="sh-form-hint">{t('admin.dashboard.frame.private_hint')}</p>
      {on && choices.length > 0 && pickedShown === 0 && (
        <p class="sh-form-hint sh-dashboard-frame__warn" role="status">
          {t('admin.dashboard.frame.pick_one')}
        </p>
      )}
    </section>
  )
}
