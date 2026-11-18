/**
 * Settings › Members & joining — how people get in, who may add things,
 * followers, @here, and (owner household) whether admins may act for the
 * owner. Every control saves the moment it changes — but one: turning
 * "Let admins act for me" on hands the space's signing seed to every
 * other household that already has an admin here, so the owner first
 * sees who they are (their household IDs) and confirms.
 *
 * Access levels (§4.3): only the levels the server applies are offered;
 * raising one that an older member household can't enforce asks "Apply
 * anyway?" (``patchSpace``). On a space hosted elsewhere the host applies
 * the change, so the select snaps back to the level in force.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { HouseholdId } from '@/components/HouseholdId'
import { RadioCardGroup } from '@/components/RadioCardGroup'
import { hasJoinModeChoice, joinOptionsForVisibility } from '@/components/spaceModeOptions'
import { membersMeta, type SpaceMemberSummary } from '@/components/SpaceSectionNav'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { instanceConfig } from '@/store/instance'
import type { Space, SpaceAccessLevel } from '@/types'
import { firstName, personLabel } from '@/utils/names'
import {
  ACCESS_FEATURES, accessLevel, featureLabel, levelLabel, levelOptions,
  type AccessFeature,
} from '@/features/spaces/spaceAccess'
import { patchSpace, useServerValue } from './spacePatch'
import { LinkRow, SwitchRow } from './SettingsRows'

interface Props {
  space: Space
  isRemoteSpace: boolean
  members: SpaceMemberSummary | null
  onUpdate: () => void
}

/** The section a feature's access row belongs to — a feature that is off
 *  has no row. */
function featureOn(space: Space, feature: AccessFeature): boolean {
  const f = space.features
  switch (feature) {
    case 'posts': return true
    case 'pages': return f?.pages ?? true
    case 'tasks': return f?.todo ?? true
    case 'stickies': return f?.stickies ?? true
    case 'calendar': return f?.calendar ?? true
  }
}

/** Another household with at least one admin of the space. */
export interface AdminHousehold {
  instance_id: string
  /** The paired household's name, when this household knows one. */
  name: string | null
  /** Its admins, by name. */
  admins: string[]
}

interface RosterRow {
  user_id: string
  role: string
  display_name?: string | null
  instance_id?: string | null
  household_name?: string | null
}

/** The households that are handed the right to act for the space when
 *  "Let admins act for me" goes on: every OTHER household with an admin
 *  seat (``GET /api/spaces/{id}/members`` rows with an ``instance_id``),
 *  one entry per household, in roster order. */
export function adminHouseholds(rows: RosterRow[], ownInstanceId?: string | null): AdminHousehold[] {
  const out = new Map<string, AdminHousehold>()
  for (const r of rows) {
    if (r.role !== 'admin' || !r.instance_id || r.instance_id === ownInstanceId) continue
    const h = out.get(r.instance_id)
      ?? { instance_id: r.instance_id, name: firstName(r.household_name), admins: [] }
    h.admins.push(personLabel(r.display_name))
    out.set(r.instance_id, h)
  }
  return [...out.values()]
}

/** Shown in place of an instant save: who gets to act for the space. */
function AdminAuthorityConfirm({ households, onConfirm, onCancel }: {
  households: AdminHousehold[]
  onConfirm: () => void
  onCancel: () => void
}) {
  const box = useRef<HTMLDivElement>(null)
  // It appears below the switch that opened it: take the focus, so a
  // keyboard or screen-reader user lands on what needs answering.
  useEffect(() => { box.current?.focus() }, [])
  return (
    <div class="sh-admin-grant-check sh-admin-authority-confirm" role="group" tabIndex={-1}
         ref={box} aria-labelledby="sh-admin-authority-confirm-title"
         data-testid="admin-authority-confirm"
         onKeyDown={(e) => { if (e.key === 'Escape') onCancel() }}>
      <p class="sh-admin-grant-check__warning" id="sh-admin-authority-confirm-title">
        {t('space.admin_authority.confirm.title')}
      </p>
      <p class="sh-admin-grant-check__hint">{t('space.admin_authority.confirm.body')}</p>
      <ul class="sh-admin-authority-confirm__list">
        {households.map(h => (
          <li key={h.instance_id}>
            <span class="sh-admin-authority-confirm__who">
              <strong>{h.admins.join(', ')}</strong>
              {h.name && <span class="sh-muted">{' · '}{h.name}</span>}
            </span>
            <span class="sh-admin-grant-check__label">{t('connections.detail.household_id')}</span>
            <HouseholdId id={h.instance_id} />
          </li>
        ))}
      </ul>
      <div class="sh-admin-authority-confirm__actions">
        <Button type="button" variant="secondary" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="button" onClick={onConfirm}>{t('space.admin_authority.confirm.turn_on')}</Button>
      </div>
    </div>
  )
}

export function MembersSection({ space, isRemoteSpace, members, onUpdate }: Props) {
  const [joinMode, setJoinMode] = useServerValue(space.join_mode)
  const f = space.features
  const [followers, setFollowers] = useServerValue(Boolean(f?.allow_subscribers))
  const [react, setReact] = useServerValue(Boolean(f?.allow_subscriber_react))
  const [comment, setComment] = useServerValue(Boolean(f?.allow_subscriber_comment))
  const [here, setHere] = useServerValue(Boolean(space.allow_here_mention))
  const [delegated, setDelegated] = useServerValue(Boolean(f?.delegated_admin_authority))
  // Turning "Let admins act for me" on: the households to confirm, and
  // whether they are being looked up.
  const [toConfirm, setToConfirm] = useState<AdminHousehold[] | null>(null)
  // One lookup / one save at a time, however fast the clicks come.
  const busy = useRef(false)
  const focusDelegated = () => document.getElementById(`delegated-${space.id}`)?.focus()
  const [levels, setLevels] = useServerValue<Record<AccessFeature, SpaceAccessLevel>>(
    Object.fromEntries(ACCESS_FEATURES.map(x => [x, accessLevel(f, x)])) as Record<
      AccessFeature, SpaceAccessLevel>,
  )

  /** Flip one value: show it, save it, put it back if the save failed. */
  const save = async <T,>(
    set: (v: T) => void, next: T, before: T, body: Record<string, unknown>,
  ) => {
    set(next)
    const outcome = await patchSpace(space.id, body, { onUpdate })
    if (outcome === 'failed') set(before)
  }

  const setLevel = async (feature: AccessFeature, next: SpaceAccessLevel) => {
    const before = levels
    setLevels({ ...levels, [feature]: next })
    const outcome = await patchSpace(
      space.id, { features: { [`${feature}_access`]: next } }, { onUpdate },
    )
    // Forwarded: the host decides — show the level in force, not the ask.
    if (outcome !== 'saved') setLevels(before)
  }

  const saveDelegated = async (v: boolean) => {
    if (busy.current) return
    busy.current = true
    setToConfirm(null)
    // The note (and the focus inside it) is gone: back to the switch.
    focusDelegated()
    try {
      await save(setDelegated, v, delegated, { features: { delegated_admin_authority: v } })
    } finally {
      busy.current = false
    }
  }

  /** Off saves at once. On first asks who the admins are — read now, not
   *  from the page's copy — and shows every other household among them;
   *  unknown means no change (never hand the right over unseen). */
  const flipDelegated = async (v: boolean) => {
    if (!v) { void saveDelegated(false); return }
    if (toConfirm || busy.current) return
    busy.current = true
    let households: AdminHousehold[]
    try {
      const rows = await api.get<RosterRow[]>(`/api/spaces/${space.id}/members`)
      households = adminHouseholds(
        Array.isArray(rows) ? rows : [], instanceConfig.value?.instance_id,
      )
    } catch {
      showToast(t('space.admin_authority.confirm.load_failed'), 'error')
      return
    } finally {
      busy.current = false
    }
    if (households.length === 0) void saveDelegated(true)
    else setToConfirm(households)
  }

  return (
    <div class="sh-ss-card">
      {hasJoinModeChoice(space.space_type) ? (
        <RadioCardGroup
          legend={t('space.join.legend')}
          name={`space-join-mode-${space.id}`}
          value={joinMode}
          options={joinOptionsForVisibility(space.space_type, { local: space.local_area != null })}
          onChange={(v) => void save(
            setJoinMode, v as Space['join_mode'], joinMode, { join_mode: v },
          )}
        />
      ) : (
        <div class="sh-radio-card-group">
          <span class="sh-radio-card-group__legend">{t('space.join.legend')}</span>
          <p class="sh-muted sh-space-settings-household-note">
            {t('space.visibility.household_sub')}
          </p>
        </div>
      )}

      <div class="sh-ss-links">
        <LinkRow href={addBase(`/spaces/${space.id}/members`)}
                 title={t('space.settings_page.members.roles')}
                 meta={members ? membersMeta(members) : null}
                 testId="settings-members-link" />
      </div>

      <fieldset class="sh-form-fieldset" data-testid="space-access">
        <legend>{t('space.access.heading')}</legend>
        <p class="sh-ss-hint">{t('space.access.intro')}</p>
        {ACCESS_FEATURES.filter(x => featureOn(space, x)).map(feature => {
          const id = `sh-access-${space.id}-${feature}`
          return (
            <div key={feature} class="sh-access-row">
              <label for={id} class="sh-access-row__label">{featureLabel(feature)}</label>
              <select id={id} class="sh-select sh-access-row__select" data-feature={feature}
                      value={levels[feature]}
                      onChange={(e) => void setLevel(
                        feature, (e.target as HTMLSelectElement).value as SpaceAccessLevel,
                      )}>
                {levelOptions().map(level => (
                  <option key={level} value={level}>{levelLabel(level)}</option>
                ))}
              </select>
            </div>
          )
        })}
        <p class="sh-ss-hint sh-access-explain">{t('space.access.explain')}</p>
      </fieldset>

      {/* Followers: the first switch decides whether they may exist at all
       *  (it makes a public / global space's posts public); the two below
       *  loosen what a follower may do. Posting stays member-only. */}
      <fieldset class="sh-form-fieldset" data-testid="space-followers">
        <legend>{t('space.followers.legend')}</legend>
        <p class="sh-ss-hint">{t('space.followers.intro')}</p>
        <SwitchRow id={`followers-${space.id}`} label={t('space.followers.allow')}
                   checked={followers}
                   onChange={(v) => void save(setFollowers, v, followers,
                     { features: { allow_subscribers: v } })} />
        <SwitchRow id={`followers-react-${space.id}`} sub label={t('space.followers.react')}
                   checked={react} disabled={!followers}
                   onChange={(v) => void save(setReact, v, react,
                     { features: { allow_subscriber_react: v } })} />
        <SwitchRow id={`followers-comment-${space.id}`} sub label={t('space.followers.comment')}
                   checked={comment} disabled={!followers}
                   onChange={(v) => void save(setComment, v, comment,
                     { features: { allow_subscriber_comment: v } })} />
        <p class="sh-ss-hint">
          {followers ? t('space.followers.members_only_note') : t('space.followers.off_note')}
        </p>
      </fieldset>

      <fieldset class="sh-form-fieldset">
        {/* ``bdi``: "@here" stays "@here" on a right-to-left page. */}
        <legend><bdi>@here</bdi></legend>
        <SwitchRow id={`here-${space.id}`} label={t('space.here.toggle')}
                   hint={t('space.here.hint')} checked={here}
                   onChange={(v) => void save(setHere, v, here, { allow_here_mention: v })} />
      </fieldset>

      {/* Owner policy switch (delegated admins); only meaningful for a
       *  space hosted here. */}
      {!isRemoteSpace && (
        <fieldset class="sh-form-fieldset">
          <legend>{t('space.admin_authority.legend')}</legend>
          <p class="sh-ss-hint">{t('space.admin_authority.intro')}</p>
          <SwitchRow id={`delegated-${space.id}`} label={t('space.admin_authority.toggle')}
                     hint={t('space.admin_authority.hint')} checked={delegated}
                     onChange={(v) => void flipDelegated(v)} />
          {toConfirm && (
            <AdminAuthorityConfirm households={toConfirm}
                                   onConfirm={() => void saveDelegated(true)}
                                   onCancel={() => { setToConfirm(null); focusDelegated() }} />
          )}
        </fieldset>
      )}
    </div>
  )
}
