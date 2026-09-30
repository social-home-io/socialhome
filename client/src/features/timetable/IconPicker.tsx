/**
 * IconPicker — the emoji a slot shows (big, in Picture view) so a
 * child who can't read yet still finds "swimming".
 *
 * A grid of localised presets (≥ 44 px, ``aria-pressed``), a
 * "No icon" option, and the shared EmojiField for any other emoji.
 */
import { useEffect, useRef } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { EmojiField } from '@/components/EmojiField'
import { t } from '@/i18n/i18n'
import { BREAK_ICONS, LESSON_ICONS, presetFor, type IconPreset } from './icons'

interface Props {
  value: string | null
  onChange: (icon: string | null) => void
  /** Unique per page — keys the EmojiField popover. */
  id: string
}

export function IconPicker({ value, onChange, id }: Props) {
  const custom = useSignal(value && !presetFor(value) ? value : '')
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const valueRef = useRef(value)
  valueRef.current = value

  // Keep the custom tile in step with a preset / none pick…
  useEffect(() => {
    if (!value || presetFor(value)) custom.value = ''
    else custom.value = value
  }, [value, custom])
  // …and report what the EmojiField sets.
  useEffect(() => custom.subscribe(v => {
    const cur = valueRef.current
    if (v && v !== cur) onChangeRef.current(v)
    else if (!v && cur && !presetFor(cur)) onChangeRef.current(null)
  }), [custom])

  const grid = (label: string, presets: readonly IconPreset[]) => (
    <div class="sh-timetable-icons__grid" role="group" aria-label={label}>
      {presets.map(p => (
        <button key={p.key} type="button" aria-pressed={value === p.emoji}
                aria-label={t(p.label)} title={t(p.label)}
                class={`sh-timetable-icons__btn${value === p.emoji ? ' is-on' : ''}`}
                onClick={() => onChange(p.emoji)}>
          <span aria-hidden="true">{p.emoji}</span>
        </button>
      ))}
    </div>
  )

  return (
    <fieldset class="sh-timetable-icons">
      <legend>{t('timetable.icon.legend')}</legend>
      {grid(t('timetable.icon.lessons'), LESSON_ICONS)}
      {grid(t('timetable.icon.extras'), BREAK_ICONS)}
      <div class="sh-timetable-icons__more">
        <button type="button" aria-pressed={value === null}
                class={`sh-chip sh-timetable-chip${value === null ? ' sh-chip--active' : ''}`}
                onClick={() => onChange(null)}>
          {t('timetable.icon.none')}
        </button>
        <EmojiField value={custom} openKey={`tt-icon-${id}`}
                    label={t('timetable.icon.custom')} hint={t('timetable.icon.custom_hint')} />
      </div>
    </fieldset>
  )
}
