/**
 * Settings › Members & joining — how people get in, who may add things,
 * followers, @here, and (owner household) whether admins may act for the
 * owner. Every control saves the moment it changes.
 *
 * Access levels (§4.3): only the levels the server applies are offered;
 * raising one that an older member household can't enforce asks "Apply
 * anyway?" (``patchSpace``). On a space hosted elsewhere the host applies
 * the change, so the select snaps back to the level in force.
 */
import { addBase } from '@/baseUrl'
import { RadioCardGroup } from '@/components/RadioCardGroup'
import { hasJoinModeChoice, joinOptionsForVisibility } from '@/components/spaceModeOptions'
import { membersMeta, type SpaceMemberSummary } from '@/components/SpaceSectionNav'
import { t } from '@/i18n/i18n'
import type { Space, SpaceAccessLevel } from '@/types'
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

export function MembersSection({ space, isRemoteSpace, members, onUpdate }: Props) {
  const [joinMode, setJoinMode] = useServerValue(space.join_mode)
  const f = space.features
  const [followers, setFollowers] = useServerValue(Boolean(f?.allow_subscribers))
  const [react, setReact] = useServerValue(Boolean(f?.allow_subscriber_react))
  const [comment, setComment] = useServerValue(Boolean(f?.allow_subscriber_comment))
  const [here, setHere] = useServerValue(Boolean(space.allow_here_mention))
  const [delegated, setDelegated] = useServerValue(Boolean(f?.delegated_admin_authority))
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

  return (
    <div class="sh-ss-card">
      {hasJoinModeChoice(space.space_type) ? (
        <RadioCardGroup
          legend={t('space.join.legend')}
          name={`space-join-mode-${space.id}`}
          value={joinMode}
          options={joinOptionsForVisibility(space.space_type)}
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
                     onChange={(v) => void save(setDelegated, v, delegated,
                       { features: { delegated_admin_authority: v } })} />
        </fieldset>
      )}
    </div>
  )
}
