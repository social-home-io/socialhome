/**
 * Settings › Sections — which sections the space shows, and which kinds
 * of posts its feed offers. Each switch saves as it flips; a section that
 * is turned off keeps its pages / events / tasks / notes / albums, which
 * come back when it is turned on again. "Edit sections" in the space's
 * More panel deep-links here.
 *
 * The map is the Location section's switch; the feed is always on.
 */
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import type { Space } from '@/types'
import { patchSpace, useServerValue } from './spacePatch'
import { SwitchRow } from './SettingsRows'

/** Section toggles in the space's section order: [feature key, label key,
 *  hint key, default when the payload omits it]. */
// For ``i18n:check``: t('space.feature.chat') t('nav.calendar')
// t('space.feature.timetable') t('nav.tasks') t('nav.pages')
// t('nav.gallery') t('nav.stickies') t('nav.bazaar')
// t('space.settings_page.sections.chat_hint') t('space.settings_page.sections.calendar_hint')
// t('space.feature.timetable_sub') t('space.settings_page.sections.tasks_hint')
// t('space.settings_page.sections.pages_hint') t('space.settings_page.sections.gallery_hint')
// t('space.settings_page.sections.stickies_hint') t('space.settings_page.sections.bazaar_hint')
const FEATURES: [keyof NonNullable<Space['features']>, string, string, boolean][] = [
  ['chat', 'space.feature.chat', 'space.settings_page.sections.chat_hint', true],
  ['calendar', 'nav.calendar', 'space.settings_page.sections.calendar_hint', true],
  ['timetable', 'space.feature.timetable', 'space.feature.timetable_sub', false],
  ['todo', 'nav.tasks', 'space.settings_page.sections.tasks_hint', true],
  ['pages', 'nav.pages', 'space.settings_page.sections.pages_hint', true],
  ['gallery', 'nav.gallery', 'space.settings_page.sections.gallery_hint', true],
  ['stickies', 'nav.stickies', 'space.settings_page.sections.stickies_hint', true],
  ['bazaar', 'nav.bazaar', 'space.settings_page.sections.bazaar_hint', true],
]

// Post types an admin can switch on / off (§23.49) — the space composer's
// set, in composer order. Types the backend tracks but that aren't
// composed via the picker (transcript / event …) are preserved untouched.
// For ``i18n:check``: t('space.post_type.text') t('space.post_type.image')
// t('space.post_type.video') t('space.post_type.file') t('space.post_type.poll')
// t('space.post_type.schedule') t('space.post_type.location')
// t('space.post_type.highlight_share')
export const SPACE_POST_TYPES: [string, string][] = [
  ['text', 'space.post_type.text'],
  ['image', 'space.post_type.image'],
  ['video', 'space.post_type.video'],
  ['file', 'space.post_type.file'],
  ['poll', 'space.post_type.poll'],
  ['schedule', 'space.post_type.schedule'],
  ['location', 'space.post_type.location'],
  ['highlight_share', 'space.post_type.highlight_share'],
]
const POST_TYPE_KEYS = SPACE_POST_TYPES.map(([k]) => k)

/** The post types to send when ``key`` flips to ``on``: the composer's
 *  chosen set plus every type the UI doesn't manage, sorted. ``null`` when
 *  that would leave no composer type on. */
export function nextAllowedPostTypes(
  stored: string[] | undefined, key: string, on: boolean,
): string[] | null {
  // A missing list = a stub the host config hasn't reached: all allowed.
  const current = stored ?? POST_TYPE_KEYS.slice()
  const preserved = current.filter(x => !POST_TYPE_KEYS.includes(x))
  const chosen = POST_TYPE_KEYS.filter(k => (k === key ? on : current.includes(k)))
  if (chosen.length === 0) return null
  return [...preserved, ...chosen].sort()
}

export function FeaturesSection({ space, onUpdate }: { space: Space; onUpdate: () => void }) {
  const f = space.features
  const [on, setOn] = useServerValue<Record<string, boolean>>(
    Object.fromEntries(FEATURES.map(([k, , , dflt]) => [k, (f?.[k] as boolean | undefined) ?? dflt])),
  )
  const [types, setTypes] = useServerValue<string[] | undefined>(f?.allowed_post_types)

  const flip = async (key: string, next: boolean) => {
    const before = on
    setOn({ ...on, [key]: next })
    const outcome = await patchSpace(space.id, { features: { [key]: next } }, { onUpdate })
    if (outcome === 'failed') setOn(before)
  }

  const flipType = async (key: string, next: boolean) => {
    const allowed = nextAllowedPostTypes(types, key, next)
    if (allowed === null) {
      showToast(t('space.post_types.none_on'), 'error')
      return
    }
    const before = types
    setTypes(allowed)
    const outcome = await patchSpace(
      space.id, { features: { allowed_post_types: allowed } }, { onUpdate },
    )
    if (outcome === 'failed') setTypes(before)
  }

  return (
    <div class="sh-ss-card">
      <div data-testid="space-features">
        {FEATURES.map(([key, label, hint]) => (
          <SwitchRow key={key} id={`feature-${key}-${space.id}`} label={t(label)}
                     hint={t(hint)} checked={on[key]} sub={key === 'timetable'}
                     onChange={(v) => void flip(key, v)} />
        ))}
      </div>
      <fieldset class="sh-form-fieldset" data-testid="space-post-types">
        <legend>{t('space.post_types.legend')}</legend>
        <p class="sh-ss-hint">{t('space.post_types.hint')}</p>
        <div class="sh-ss-checks">
          {SPACE_POST_TYPES.map(([key, label]) => (
            <label key={key} class="sh-toggle-row">
              <input type="checkbox" checked={!types || types.includes(key)}
                     onChange={(e) => void flipType(key, (e.target as HTMLInputElement).checked)} />
              {t(label)}
            </label>
          ))}
        </div>
      </fieldset>
    </div>
  )
}
