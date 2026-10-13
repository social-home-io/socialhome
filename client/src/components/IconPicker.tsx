/**
 * IconPicker — pick one emoji as an icon (a timetable slot, a space
 * zone…).
 *
 * One or more grids of localised presets (≥ 44 px, ``aria-pressed``),
 * a "No icon" option, and the shared EmojiField for any other emoji.
 * Callers pass their own preset groups and copy; the picker owns the
 * behaviour.
 */
import { useEffect, useRef } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { EmojiField } from './EmojiField'
import { t } from '@/i18n/i18n'

export interface IconPickerPreset {
  emoji: string
  key: string
  /** i18n key of the localised name. */
  label: string
}

export interface IconPickerGroup {
  /** Already-translated group label (the grid's accessible name). */
  label: string
  presets: readonly IconPickerPreset[]
}

interface Props {
  value: string | null
  onChange: (icon: string | null) => void
  /** Unique per page — keys the EmojiField popover. */
  id: string
  groups: readonly IconPickerGroup[]
  legend: string
  noneLabel: string
  customLabel: string
  customHint?: string
}

export function IconPicker({
  value, onChange, id, groups, legend, noneLabel, customLabel, customHint,
}: Props) {
  const isPreset = (v: string | null) =>
    !!v && groups.some(g => g.presets.some(p => p.emoji === v))
  const custom = useSignal(value && !isPreset(value) ? value : '')
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const valueRef = useRef(value)
  valueRef.current = value
  const isPresetRef = useRef(isPreset)
  isPresetRef.current = isPreset

  // Keep the custom tile in step with a preset / none pick…
  useEffect(() => {
    custom.value = !value || isPresetRef.current(value) ? '' : value
  }, [value, custom])
  // …and report what the EmojiField sets.
  useEffect(() => custom.subscribe(v => {
    const cur = valueRef.current
    if (v && v !== cur) onChangeRef.current(v)
    else if (!v && cur && !isPresetRef.current(cur)) onChangeRef.current(null)
  }), [custom])

  return (
    <fieldset class="sh-icon-picker">
      <legend>{legend}</legend>
      {groups.map(g => (
        <div key={g.label} class="sh-icon-picker__grid" role="group" aria-label={g.label}>
          {g.presets.map(p => (
            <button key={p.key} type="button" aria-pressed={value === p.emoji}
                    aria-label={t(p.label)} title={t(p.label)}
                    class={`sh-icon-picker__btn${value === p.emoji ? ' is-on' : ''}`}
                    onClick={() => onChange(p.emoji)}>
              <span aria-hidden="true">{p.emoji}</span>
            </button>
          ))}
        </div>
      ))}
      <div class="sh-icon-picker__more">
        <button type="button" aria-pressed={value === null}
                class={`sh-chip sh-icon-picker__none${value === null ? ' sh-chip--active' : ''}`}
                onClick={() => onChange(null)}>
          {noneLabel}
        </button>
        <EmojiField value={custom} openKey={id}
                    label={customLabel} hint={customHint} />
      </div>
    </fieldset>
  )
}
