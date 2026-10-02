/**
 * HouseholdThemeStudio — household-wide theme editor (§23.125).
 *
 * Pairs with SpaceThemeStudio. Hits PUT /api/theme (admin-only). The
 * household name is edited separately in admin Settings (the single
 * source of truth), so this studio only owns the look-and-feel.
 *
 * Surface mirrors :mod:`SpaceThemeStudio` (presets row + font picker are
 * shared via ``ThemeStudioControls``): a row of preset swatches at
 * the top for one-click "make it look nice", a tighter form grid for
 * the colour pickers, and a live preview card showing how a feed post
 * will read with the current values.  The household theme is the
 * default that every space inherits from, so this view earns at least
 * the same polish as the per-space studio rather than the bare-slider
 * stack it used to ship as.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { householdFont } from '@/store/householdTheme'
import { Button } from './Button'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { BRAND_ACCENT, BRAND_PRIMARY } from '@/utils/themeBrand'
import { APP_FONT_STACK, FONT_STACKS, isFontId, type FontId } from '@/utils/themeFonts'
import { ThemeFontPicker, ThemePresetRow, type ThemePreset } from './ThemeStudioControls'

type Mode     = 'light' | 'dark' | 'auto'
type Density  = 'compact' | 'comfortable' | 'spacious'

interface HouseholdTheme {
  primary_color: string
  accent_color:  string
  surface_color: string | null
  surface_dark:  string | null
  mode:          Mode
  font_family:   FontId
  density:       Density
  corner_radius: number
}

// Initial signals match the brand defaults in ``tokens.css`` and the
// ``household_theme`` schema row — opening the studio and saving
// without changing the colours leaves the SPA on the warm hearth
// palette instead of flipping ``--sh-primary`` to legacy cold blue.
const primary       = signal(BRAND_PRIMARY)
const accent        = signal(BRAND_ACCENT)
const surface       = signal<string>('')          // '' = unset
const surfaceDark   = signal<string>('')
const mode          = signal<Mode>('auto')
const font          = signal<FontId>('system')
const density       = signal<Density>('comfortable')
const cornerRadius  = signal<number>(12)
const loading       = signal(true)
const saving        = signal(false)

function applyPreset(p: ThemePreset) {
  primary.value = p.primary
  accent.value  = p.accent
  surface.value = p.surface ?? ''
}

export function HouseholdThemeStudio() {
  useEffect(() => { void load() }, [])

  if (loading.value) return <Spinner />

  return (
    <section class="sh-theme-studio sh-household-theme-studio">
      <h3 style={{ margin: 0 }}>{t('theme.household.title')}</h3>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
        {t('theme.household.intro')}
      </p>

      <ThemePresetRow onApply={applyPreset} />

      <div class="sh-theme-studio-grid">
        <label>
          {t('theme.primary')}
          <input type="color" value={primary.value}
                 onInput={(e) => (primary.value = (e.target as HTMLInputElement).value)} />
        </label>
        <label>
          {t('theme.accent')}
          <input type="color" value={accent.value}
                 onInput={(e) => (accent.value = (e.target as HTMLInputElement).value)} />
        </label>
        <label>
          {t('theme.light_surface')}
          <input type="color" value={surface.value || '#ffffff'}
                 onInput={(e) => (surface.value = (e.target as HTMLInputElement).value)} />
          {surface.value && (
            <button type="button" class="sh-link"
                    onClick={() => (surface.value = '')}>
              {t('theme.clear')}
            </button>
          )}
        </label>
        <label>
          {t('theme.dark_surface')}
          <input type="color" value={surfaceDark.value || '#101820'}
                 onInput={(e) => (surfaceDark.value = (e.target as HTMLInputElement).value)} />
          {surfaceDark.value && (
            <button type="button" class="sh-link"
                    onClick={() => (surfaceDark.value = '')}>
              {t('theme.clear')}
            </button>
          )}
        </label>
        <label>
          {t('theme.mode')}
          <select value={mode.value}
                  onChange={(e) =>
                    (mode.value = (e.target as HTMLSelectElement).value as Mode)}>
            <option value="auto">{t('theme.mode.auto')}</option>
            <option value="light">{t('theme.mode.light')}</option>
            <option value="dark">{t('theme.mode.dark')}</option>
          </select>
        </label>
        <label>
          {t('theme.density')}
          <select value={density.value}
                  onChange={(e) =>
                    (density.value = (e.target as HTMLSelectElement).value as Density)}>
            <option value="compact">{t('theme.density.compact')}</option>
            <option value="comfortable">{t('theme.density.comfortable')}</option>
            <option value="spacious">{t('theme.density.spacious')}</option>
          </select>
        </label>
        <label>
          {t('theme.corner_radius', { px: String(cornerRadius.value) })}
          <input type="range" min={0} max={24} step={1}
                 value={cornerRadius.value}
                 onInput={(e) =>
                   (cornerRadius.value = parseInt(
                     (e.target as HTMLInputElement).value, 10,
                   ))} />
        </label>
      </div>

      <ThemeFontPicker
        name="household-theme-font" value={font.value}
        onChange={id => (font.value = id)}
        system={{
          // ``system`` = no override → the app's own body font.
          title: t('theme.font.default'),
          hint: t('theme.font.default_hint'),
          stack: APP_FONT_STACK,
        }}
      />

      {/* Live preview card — same shape as the per-space studio so
       *  the two surfaces feel like cousins.  Sets CSS variables
       *  inline so we don't have to flush ``applyToDocument`` until
       *  the user actually saves. */}
      <div class="sh-theme-studio-preview"
           style={{
             '--preview-primary': primary.value,
             '--preview-accent': accent.value,
             '--preview-tint': surface.value || 'transparent',
             fontFamily: font.value === 'system' ? APP_FONT_STACK : FONT_STACKS[font.value],
           } as Record<string, string>}>
        <div class="sh-theme-preview-card">
          <div class="sh-theme-preview-header">
            <span class="sh-theme-preview-dot" />
            <strong>{t('theme.preview')}</strong>
          </div>
          <p>{t('theme.household.preview_text')}</p>
          <div class="sh-theme-preview-chip">👍 3</div>
        </div>
      </div>

      <div class="sh-form-actions">
        <Button onClick={save} disabled={saving.value}>
          {saving.value ? t('theme.saving') : t('common.save')}
        </Button>
      </div>
    </section>
  )
}

async function load() {
  loading.value = true
  try {
    const theme = await api.get('/api/theme') as HouseholdTheme
    primary.value      = theme.primary_color
    accent.value       = theme.accent_color
    surface.value      = theme.surface_color ?? ''
    surfaceDark.value  = theme.surface_dark  ?? ''
    mode.value         = theme.mode
    font.value         = isFontId(theme.font_family) ? theme.font_family : 'system'
    density.value      = theme.density
    cornerRadius.value = theme.corner_radius
    applyToDocument()
  } catch (err: unknown) {
    showToast(
      t('theme.household.load_failed', { error: String((err as Error)?.message ?? err) }),
      'error',
    )
  } finally {
    loading.value = false
  }
}

// §23.125.4 — write the spec-mandated `--hh-*` custom properties on
// :root so the live page reflects the new theme without a reload.
function applyToDocument() {
  const r = document.documentElement.style
  r.setProperty('--hh-accent',       accent.value)
  if (surface.value)     r.setProperty('--hh-surface',      surface.value)
  if (surfaceDark.value) r.setProperty('--hh-surface-dark', surfaceDark.value)
  r.setProperty('--hh-radius-card', `${cornerRadius.value}px`)
  r.setProperty('--hh-radius-btn',  `${cornerRadius.value}px`)
  const gapMap: Record<Density, string> = {
    compact:     '0.5rem',
    comfortable: '1rem',
    spacious:    '1.5rem',
  }
  r.setProperty('--hh-density-gap', gapMap[density.value])
}

async function save() {
  saving.value = true
  try {
    await api.put('/api/theme', {
      primary_color: primary.value,
      accent_color:  accent.value,
      surface_color: surface.value || null,
      surface_dark:  surfaceDark.value || null,
      mode:          mode.value,
      font_family:   font.value,
      density:       density.value,
      corner_radius: cornerRadius.value,
    })
    applyToDocument()
    // The household font is painted app-wide by the store.
    householdFont.value = font.value
    showToast(t('theme.saved'), 'success')
  } catch (err: unknown) {
    showToast(
      t('theme.save_failed', { error: String((err as Error)?.message ?? err) }),
      'error',
    )
  } finally {
    saving.value = false
  }
}
