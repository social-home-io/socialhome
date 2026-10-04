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
 *  GFS label map (global_server/public.py). ``value`` is what gets stored and
 *  sent; ``label`` is a getter, so it follows the UI language. */
function category(value: string, label: () => string): { value: string; readonly label: string } {
  return { value, get label() { return label() } }
}

export const SPACE_CATEGORIES: { value: string; readonly label: string }[] = [
  category('general',          () => t('space.category.general')),
  category('hobby_crafts',     () => t('space.category.hobby_crafts')),
  category('sports_outdoors',  () => t('space.category.sports_outdoors')),
  category('gaming',           () => t('space.category.gaming')),
  category('music_arts',       () => t('space.category.music_arts')),
  category('food_drink',       () => t('space.category.food_drink')),
  category('tech',             () => t('space.category.tech')),
  category('local',            () => t('space.category.local')),
  category('family_parenting', () => t('space.category.family_parenting')),
  category('learning',         () => t('space.category.learning')),
]

/** Map any value (unknown/legacy/empty/null) to a display label; default General. */
export function categoryLabel(value: string | null | undefined): string {
  return SPACE_CATEGORIES.find(c => c.value === value)?.label ?? t('space.category.general')
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
