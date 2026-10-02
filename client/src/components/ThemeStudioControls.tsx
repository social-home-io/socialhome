/**
 * Controls shared by :mod:`HouseholdThemeStudio` and
 * :mod:`SpaceThemeStudio`, so the two studios offer the same presets
 * and the same font choices in the same words.
 *
 * Fonts are schema ids (``system | serif | rounded | mono``) — the
 * picker shows each one in its real stack from ``utils/themeFonts`` and
 * reports the id, never a CSS value, so whatever it emits is what the
 * server accepts.
 */
import type { JSX } from 'preact'
import { t } from '@/i18n/i18n'
import { BRAND_ACCENT, BRAND_PRIMARY } from '@/utils/themeBrand'
import { FONT_IDS, FONT_STACKS, type FontId } from '@/utils/themeFonts'
import { RadioCardGroup, type RadioCardOption } from './RadioCardGroup'

export interface ThemePreset {
  id: 'default' | 'calm' | 'bold' | 'playful' | 'high_contrast'
  primary: string
  accent: string
  /** Surface / background tint — ``null`` keeps the brand cream. */
  surface: string | null
}

/** Quick-apply palettes. ``default`` is the brand hearth + honey from
 *  ``tokens.css``. */
export const THEME_PRESETS: readonly ThemePreset[] = [
  { id: 'default',       primary: BRAND_PRIMARY, accent: BRAND_ACCENT, surface: null },
  { id: 'calm',          primary: '#5D7CBB', accent: '#70B3A4', surface: '#F0F4F8' },
  { id: 'bold',          primary: '#E94E77', accent: '#FFB400', surface: '#1F1E26' },
  { id: 'playful',       primary: '#B14AED', accent: '#FFD447', surface: '#FDF4FF' },
  { id: 'high_contrast', primary: '#000000', accent: '#0050E6', surface: '#FFFFFF' },
]

const PRESET_LABEL_KEYS: Record<ThemePreset['id'], string> = {
  default:       'theme.preset.default',
  calm:          'theme.preset.calm',
  bold:          'theme.preset.bold',
  playful:       'theme.preset.playful',
  high_contrast: 'theme.preset.high_contrast',
}

export function ThemePresetRow(
  { onApply }: { onApply: (p: ThemePreset) => void },
): JSX.Element {
  return (
    <div class="sh-theme-presets" role="group" aria-label={t('theme.presets')}>
      {THEME_PRESETS.map(p => {
        const label = t(PRESET_LABEL_KEYS[p.id])
        return (
          <button key={p.id} type="button"
                  class="sh-theme-preset"
                  onClick={() => onApply(p)}
                  title={label}>
            <span class="sh-theme-preset-swatch"
                  style={{
                    background: `linear-gradient(135deg, ${p.primary} 0 50%, ${p.accent} 50% 100%)`,
                  }}
                  aria-hidden="true" />
            <span>{label}</span>
          </button>
        )
      })}
    </div>
  )
}

const FONT_KEYS: Record<Exclude<FontId, 'system'>, { title: string, hint: string }> = {
  serif:   { title: 'theme.font.serif',   hint: 'theme.font.serif_hint' },
  rounded: { title: 'theme.font.rounded', hint: 'theme.font.rounded_hint' },
  mono:    { title: 'theme.font.mono',    hint: 'theme.font.mono_hint' },
}

/** How the ``system`` id (no override) reads at a given scope: the app
 *  font for the household, "same as household" inside a space. */
export interface SystemFontChoice {
  title: string
  hint: string
  /** The stack ``system`` really paints with at this scope. */
  stack: string
}

/** The font options — exported so a test can assert the ids. */
export function fontOptions(system: SystemFontChoice): RadioCardOption[] {
  return FONT_IDS.map(id => id === 'system'
    ? { value: id, icon: 'Aa', title: system.title, subtitle: system.hint, fontFamily: system.stack }
    : {
        value: id,
        icon: 'Aa',
        title: t(FONT_KEYS[id].title),
        subtitle: t(FONT_KEYS[id].hint),
        fontFamily: FONT_STACKS[id],
      })
}

export function ThemeFontPicker({ name, value, onChange, system }: {
  name: string
  value: FontId
  onChange: (id: FontId) => void
  system: SystemFontChoice
}): JSX.Element {
  return (
    <RadioCardGroup
      legend={t('theme.font')}
      name={name}
      value={value}
      options={fontOptions(system)}
      onChange={v => onChange(v as FontId)}
    />
  )
}
