/**
 * Shared option lists for the space visibility + join-mode radio-card groups
 * (used by SpaceCreateDialog and SpaceSettings).
 */
import { t } from '@/i18n/i18n'
import type { RadioCardOption } from './RadioCardGroup'

// Public requires a map location (the create dialog collects one when this
// is chosen; the backend 422s without it). A function, not a constant, so
// the labels follow the UI language.
export function visibilityOptions(): RadioCardOption[] {
  return [
    {
      value: 'private',
      icon: '🔒',
      title: t('space.visibility.private'),
      subtitle: t('space.visibility.private_sub'),
    },
    {
      value: 'household',
      icon: '🏠',
      title: t('space.visibility.household'),
      subtitle: t('space.visibility.household_sub'),
    },
    {
      value: 'public',
      icon: '🌐',
      title: t('space.visibility.public'),
      subtitle: t('space.visibility.public_sub'),
    },
    {
      value: 'global',
      icon: '🌍',
      title: t('space.visibility.global'),
      subtitle: t('space.visibility.global_sub'),
    },
  ]
}

/** Discovery categories (§23.50) — shown for public/global spaces. Values
 *  mirror the backend ``SPACE_CATEGORIES`` (socialhome/domain/space.py) and the
 *  GFS label map (global_server/public.py). */
export const SPACE_CATEGORIES: { value: string; label: string }[] = [
  { value: 'general',          label: 'General' },
  { value: 'hobby_crafts',     label: 'Hobby & crafts' },
  { value: 'sports_outdoors',  label: 'Sports & outdoors' },
  { value: 'gaming',           label: 'Gaming' },
  { value: 'music_arts',       label: 'Music & arts' },
  { value: 'food_drink',       label: 'Food & drink' },
  { value: 'tech',             label: 'Tech' },
  { value: 'local',            label: 'Local / neighborhood' },
  { value: 'family_parenting', label: 'Family & parenting' },
  { value: 'learning',         label: 'Learning' },
]

/** Map any value (unknown/legacy/empty/null) to a display label; default General. */
export function categoryLabel(value: string | null | undefined): string {
  return SPACE_CATEGORIES.find(c => c.value === value)?.label ?? 'General'
}

/** The join modes, in the UI language. */
export function joinModeOptions(): RadioCardOption[] {
  return [
    {
      value: 'invite_only',
      icon: '✉️',
      title: t('space.join.invite_only'),
      subtitle: t('space.join.invite_only_sub'),
    },
    {
      value: 'request',
      icon: '🙋',
      title: t('space.join.request'),
      subtitle: t('space.join.request_sub'),
    },
    {
      value: 'open',
      icon: '🔓',
      title: t('space.join.open'),
      subtitle: t('space.join.open_sub'),
    },
  ]
}

/**
 * Join-mode options for a given visibility. A **private** space is
 * invite-only by definition (you can't request or openly join something
 * hidden), so the non-invite options are shown but disabled — the
 * constraint is visible rather than hidden.
 */
export function joinOptionsForVisibility(spaceType: string): RadioCardOption[] {
  const options = joinModeOptions()
  if (spaceType !== 'private') return options
  return options.map((o) =>
    o.value === 'invite_only' ? o : { ...o, disabled: true },
  )
}
