/**
 * AudiencePicker — "Who can see today's highlight": all paired
 * households / your household only / choose households or people….
 *
 * One picker for the composer's first frame of the day and the "Change
 * for all of today…" sheet, so the two read the same. Choosing needs at
 * least one pick (B21): the option says "Pick at least one" until then,
 * and the caller keeps its action disabled (``choiceIsComplete``).
 * Rows are full-width labels ≥ 52 px tall; the households / people
 * switch and every checkbox row are ≥ 36 px.
 */
import type { ComponentChildren } from 'preact'
import { Button } from '@/components/Button'
import { t, isOne } from '@/i18n/i18n'
import {
  nameList,
  type AudienceChoice,
  type AudienceMode,
  type ConnectedPerson,
  type RemoteHousehold,
} from './todayAudience'

interface Props {
  /** Radio group name — unique per picker on the page. */
  name: string
  value: AudienceChoice
  onChange: (next: AudienceChoice) => void
  households: RemoteHousehold[]
  people: ConnectedPerson[]
  /** This household's name and how many people live here (the author
   *  included), for "Your household only". */
  ownName: string
  ownCount: number
  /** Why the lists couldn't load; shown with a Retry. */
  loadError: string | null
  onRetry: () => void
  /** A line under an option (the sheet's "Now" / what it changes). */
  sub?: (mode: AudienceMode) => ComponentChildren
}

export function AudiencePicker(props: Props) {
  const { value, onChange, households, people } = props
  const set = (patch: Partial<AudienceChoice>) => onChange({ ...value, ...patch })
  const picked = value.chooseKind === 'households' ? value.households : value.users
  const toggle = (id: string) => {
    const list = picked.includes(id) ? picked.filter(x => x !== id) : [...picked, id]
    set(value.chooseKind === 'households' ? { households: list } : { users: list })
  }
  const nameOf = (id: string): string =>
    people.find(p => p.user_id === id)?.display_name
    ?? households.find(h => h.instance_id === id)?.display_name
    ?? id

  const chooseSub = picked.length === 0
    ? <span class={value.mode === 'choose' ? 'sh-hlc-pick__warn' : undefined}>{t('highlight.composer.pick_one')}</span>
    : nameList(picked.map(nameOf))

  const row = (mode: AudienceMode, title: string, sub: ComponentChildren) => (
    <label class={`sh-hlc-pick__row${value.mode === mode ? ' sh-hlc-pick__row--on' : ''}`}>
      <input
        type="radio"
        name={props.name}
        value={mode}
        checked={value.mode === mode}
        onChange={() => set({ mode })}
      />
      <span class="sh-hlc-pick__text">
        <span class="sh-hlc-pick__title">{title}</span>
        <span class="sh-hlc-pick__sub">{props.sub?.(mode) ?? sub}</span>
      </span>
    </label>
  )

  const list = value.chooseKind === 'households'
    ? households.map(h => ({ id: h.instance_id, label: h.display_name, meta: null as string | null }))
    : people.map(p => ({ id: p.user_id, label: p.display_name, meta: p.household_name }))

  return (
    <div class="sh-hlc-pick" role="radiogroup" aria-label={t('highlight.composer.audience_title')}>
      {row('all', t('highlight.composer.audience_all'), null)}
      {row(
        'household',
        t('highlight.composer.audience_household'),
        props.ownName
          ? t(isOne(props.ownCount) ? 'highlight.composer.household_sub_one' : 'highlight.composer.household_sub',
            { name: props.ownName, n: String(props.ownCount) })
          : null,
      )}
      {row('choose', t('highlight.composer.audience_choose'), chooseSub)}
      {value.mode === 'choose' && (
        <div class="sh-hlc-pick__choose">
          <div class="sh-hlc-pick__kinds">
            <button
              type="button"
              class="sh-hlc-pick__kind"
              aria-pressed={value.chooseKind === 'households'}
              onClick={() => set({ chooseKind: 'households' })}
            >{t('highlight.composer.choose_households')}</button>
            <button
              type="button"
              class="sh-hlc-pick__kind"
              aria-pressed={value.chooseKind === 'users'}
              onClick={() => set({ chooseKind: 'users' })}
            >{t('highlight.composer.choose_people')}</button>
          </div>
          {props.loadError ? (
            <div role="alert" class="sh-hlc-pick__error">
              <p class="sh-muted">
                {t('highlight.composer.audience_failed', { error: props.loadError })}
              </p>
              <Button type="button" variant="secondary" onClick={props.onRetry}>
                {t('common.retry')}
              </Button>
            </div>
          ) : list.length === 0 ? (
            <p class="sh-muted">
              {t(value.chooseKind === 'households'
                ? 'highlight.composer.no_households'
                : 'highlight.composer.no_people')}
            </p>
          ) : (
            <div class="sh-hlc-pick__list">
              {list.map(c => (
                <label key={c.id} class="sh-hlc-pick__check">
                  <input
                    type="checkbox"
                    checked={picked.includes(c.id)}
                    onChange={() => toggle(c.id)}
                  />
                  <span dir="auto">{c.label}</span>
                  {c.meta && <span class="sh-muted" dir="auto"> · {c.meta}</span>}
                </label>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
