/**
 * HouseholdThemeStudio — household-wide theme editor (§23.125).
 *
 * Pairs with SpaceThemeStudio. Hits PUT /api/theme (admin-only). The
 * household name is edited separately in admin Settings (the single
 * source of truth), so this studio only owns the look-and-feel.
 *
 * Surface mirrors :mod:`SpaceThemeStudio`: a row of preset swatches at
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
import { Button } from './Button'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { FONT_STACKS, fontStack, type FontId } from '@/utils/themeFonts'

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

interface Preset {
  label: string
  primary: string
  accent: string
  /** Optional surface tint — leave ``null`` to keep the brand cream. */
  surface: string | null
}

/** Quick-apply palettes — same names as :mod:`SpaceThemeStudio` so a
 *  household admin who's used the per-space studio recognises the
 *  language.  ``Default`` is the brand hearth+honey; ``Calm`` cools
 *  to a soft blue/teal; ``Bold`` is a punchy magenta on near-black;
 *  ``Playful`` is purple on lavender; ``High contrast`` is the
 *  accessibility-first option. */
const PRESETS: Preset[] = [
  { label: 'Default',       primary: '#D2542A', accent: '#C8902F', surface: null },
  { label: 'Calm',          primary: '#5D7CBB', accent: '#70B3A4', surface: '#F0F4F8' },
  { label: 'Bold',          primary: '#E94E77', accent: '#FFB400', surface: '#1F1E26' },
  { label: 'Playful',       primary: '#B14AED', accent: '#FFD447', surface: '#FDF4FF' },
  { label: 'High contrast', primary: '#000000', accent: '#0050E6', surface: '#FFFFFF' },
]

// Initial signals match the brand defaults in ``tokens.css`` and the
// ``household_theme`` schema row — opening the studio and saving
// without changing the colours leaves the SPA on the warm hearth
// palette instead of flipping ``--sh-primary`` to legacy cold blue.
const primary       = signal('#D2542A')
const accent        = signal('#C8902F')
const surface       = signal<string>('')          // '' = unset
const surfaceDark   = signal<string>('')
const mode          = signal<Mode>('auto')
const font          = signal<FontId>('system')
const density       = signal<Density>('comfortable')
const cornerRadius  = signal<number>(12)
const loading       = signal(true)
const saving        = signal(false)

function applyPreset(p: Preset) {
  primary.value = p.primary
  accent.value  = p.accent
  surface.value = p.surface ?? ''
}

export function HouseholdThemeStudio() {
  useEffect(() => { void load() }, [])

  if (loading.value) return <Spinner />

  return (
    <section class="sh-theme-studio sh-household-theme-studio">
      <h3 style={{ margin: 0 }}>Household appearance</h3>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
        Sets the default look for every surface in this household.
        Spaces inherit unless an admin overrides them.
      </p>

      <div class="sh-theme-presets" role="group" aria-label="Theme presets">
        {PRESETS.map(p => (
          <button key={p.label} type="button"
                  class="sh-theme-preset"
                  onClick={() => applyPreset(p)}
                  title={p.label}>
            <span class="sh-theme-preset-swatch"
                  style={{
                    background: `linear-gradient(135deg, ${p.primary} 0 50%, ${p.accent} 50% 100%)`,
                  }}
                  aria-hidden="true" />
            <span>{p.label}</span>
          </button>
        ))}
      </div>

      <div class="sh-theme-studio-grid">
        <label>
          Primary
          <input type="color" value={primary.value}
                 onInput={(e) => (primary.value = (e.target as HTMLInputElement).value)} />
        </label>
        <label>
          Accent
          <input type="color" value={accent.value}
                 onInput={(e) => (accent.value = (e.target as HTMLInputElement).value)} />
        </label>
        <label>
          Light surface
          <input type="color" value={surface.value || '#ffffff'}
                 onInput={(e) => (surface.value = (e.target as HTMLInputElement).value)} />
          {surface.value && (
            <button type="button" class="sh-link"
                    onClick={() => (surface.value = '')}>
              Clear
            </button>
          )}
        </label>
        <label>
          Dark surface
          <input type="color" value={surfaceDark.value || '#101820'}
                 onInput={(e) => (surfaceDark.value = (e.target as HTMLInputElement).value)} />
          {surfaceDark.value && (
            <button type="button" class="sh-link"
                    onClick={() => (surfaceDark.value = '')}>
              Clear
            </button>
          )}
        </label>
        <label>
          Mode
          <select value={mode.value}
                  onChange={(e) =>
                    (mode.value = (e.target as HTMLSelectElement).value as Mode)}>
            <option value="auto">Auto (system)</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </label>
        <label>
          Font
          <select value={font.value}
                  onChange={(e) =>
                    (font.value = (e.target as HTMLSelectElement).value as FontId)}>
            <option value="system">System</option>
            <option value="serif">Serif</option>
            <option value="rounded">Rounded</option>
            <option value="mono">Mono</option>
          </select>
        </label>
        <label>
          Density
          <select value={density.value}
                  onChange={(e) =>
                    (density.value = (e.target as HTMLSelectElement).value as Density)}>
            <option value="compact">Compact</option>
            <option value="comfortable">Comfortable</option>
            <option value="spacious">Spacious</option>
          </select>
        </label>
        <label>
          Corner radius ({cornerRadius.value}px)
          <input type="range" min={0} max={24} step={1}
                 value={cornerRadius.value}
                 onInput={(e) =>
                   (cornerRadius.value = parseInt(
                     (e.target as HTMLInputElement).value, 10,
                   ))} />
        </label>
      </div>

      {/* Live preview card — same shape as the per-space studio so
       *  the two surfaces feel like cousins.  Sets CSS variables
       *  inline so we don't have to flush ``applyToDocument`` until
       *  the user actually saves. */}
      <div class="sh-theme-studio-preview"
           style={{
             '--preview-primary': primary.value,
             '--preview-accent': accent.value,
             '--preview-tint': surface.value || 'transparent',
           } as Record<string, string>}>
        <div class="sh-theme-preview-card">
          <div class="sh-theme-preview-header">
            <span class="sh-theme-preview-dot" />
            <strong>Preview</strong>
          </div>
          <p>Your household feed will look like this.</p>
          <div class="sh-theme-preview-chip">👍 3</div>
        </div>
      </div>

      <div class="sh-form-actions">
        <Button onClick={save} disabled={saving.value}>
          {saving.value ? 'Saving…' : 'Save'}
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
    font.value         = theme.font_family
    density.value      = theme.density
    cornerRadius.value = theme.corner_radius
    applyToDocument()
  } catch (err: unknown) {
    showToast(
      `Could not load theme: ${(err as Error)?.message ?? err}`,
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
  r.setProperty('--hh-font', fontStack(font.value) ?? FONT_STACKS.system)
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
    showToast('Saved', 'success')
  } catch (err: unknown) {
    showToast(
      `Save failed: ${(err as Error)?.message ?? err}`,
      'error',
    )
  } finally {
    saving.value = false
  }
}
