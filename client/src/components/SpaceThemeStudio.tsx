/**
 * SpaceThemeStudio — per-space theming studio (§23.123 / §23.124).
 *
 * Mirrors the :mod:`HouseholdThemeStudio` layout at space scope (the
 * presets row + font picker are shared, ``ThemeStudioControls``):
 * primary + accent colour pickers, a background tint, mode override,
 * font, post layout, and a live preview of all of them. Saves via
 * ``PUT /api/spaces/{id}/theme``; the space feed's
 * :func:`useSpaceTheme` hook paints the saved values on its next mount.
 *
 * Font and layout are schema ids (``utils/themeFonts`` /
 * ``utils/themeLayouts``) — the studio offers exactly the values the
 * server accepts and sends the id, never a CSS value.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { householdFontStack } from '@/store/householdTheme'
import { t } from '@/i18n/i18n'
import { BRAND_ACCENT, BRAND_PRIMARY } from '@/utils/themeBrand'
import { FONT_STACKS, isFontId, type FontId } from '@/utils/themeFonts'
import { DEFAULT_LAYOUT, LAYOUT_IDS, isLayoutId, type LayoutId } from '@/utils/themeLayouts'
import { Button } from './Button'
import { RadioCardGroup, type RadioCardOption } from './RadioCardGroup'
import { Spinner } from './Spinner'
import { ThemeFontPicker, ThemePresetRow, type ThemePreset } from './ThemeStudioControls'
import { showToast } from './Toast'

interface SpaceThemeResponse {
  primary_color?: string | null
  accent_color?: string | null
  background_tint?: string | null
  mode_override?: string | null
  font_family?: string | null
  post_layout?: string | null
  /** True when the space has no theme row and the GET fell back to the
   *  household theme — its font is not a space override. */
  is_default?: boolean
}

type ModeChoice = 'inherit' | 'light' | 'dark'


const LAYOUT_META: Record<LayoutId, { icon: string, title: string, hint: string }> = {
  card:     { icon: '🗂️', title: 'theme.layout.card',     hint: 'theme.layout.card_hint' },
  compact:  { icon: '☰',  title: 'theme.layout.compact',  hint: 'theme.layout.compact_hint' },
  magazine: { icon: '📰', title: 'theme.layout.magazine', hint: 'theme.layout.magazine_hint' },
}

function layoutOptions(): RadioCardOption[] {
  return LAYOUT_IDS.map(id => ({
    value: id,
    icon: LAYOUT_META[id].icon,
    title: t(LAYOUT_META[id].title),
    subtitle: t(LAYOUT_META[id].hint),
  }))
}

function asMode(v: string | null | undefined): ModeChoice {
  return v === 'light' || v === 'dark' ? v : 'inherit'
}

interface Props {
  spaceId: string
  onSaved?: () => void
}

export function SpaceThemeStudio({ spaceId, onSaved }: Props) {
  const [primary, setPrimary] = useState(BRAND_PRIMARY)
  const [accent, setAccent] = useState(BRAND_ACCENT)
  const [tint, setTint] = useState<string>('')          // '' means unset
  const [mode, setMode] = useState<ModeChoice>('inherit')
  const [font, setFont] = useState<FontId>('system')
  const [layout, setLayout] = useState<LayoutId>(DEFAULT_LAYOUT)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [saving, setSaving] = useState(false)

  // Stale-response guard (the ``stopped`` pattern of useSpaceTheme): a
  // newer load — another space, a retry — or unmount stops the older
  // one, so a slow response can never paint over the current space.
  const run = useRef<{ stopped: boolean } | null>(null)

  const load = async () => {
    if (run.current) run.current.stopped = true
    const mine = { stopped: false }
    run.current = mine
    setStatus('loading')
    try {
      const th = await api.get(`/api/spaces/${spaceId}/theme`) as SpaceThemeResponse
      if (mine.stopped) return
      if (th.is_default) {
        // No space theme yet — the GET fell back to the HOUSEHOLD row.
        // Start from the brand defaults (= "no override" everywhere)
        // so saving doesn't pin the household's current colours / font
        // onto this space as if they were its own.
        setPrimary(BRAND_PRIMARY)
        setAccent(BRAND_ACCENT)
        setTint('')
        setMode('inherit')
        setFont('system')
        setLayout(DEFAULT_LAYOUT)
      } else {
        setPrimary(th.primary_color || BRAND_PRIMARY)
        setAccent(th.accent_color || BRAND_ACCENT)
        setTint(th.background_tint ?? '')
        setMode(asMode(th.mode_override))
        // The schema CHECK keeps stored values inside the id sets; anything
        // else starts from the space defaults instead of being echoed back
        // into a save the server would refuse.
        setFont(isFontId(th.font_family) ? th.font_family : 'system')
        setLayout(isLayoutId(th.post_layout) ? th.post_layout : DEFAULT_LAYOUT)
      }
      setStatus('ready')
    } catch {
      if (mine.stopped) return
      // Never offer a save here: it would overwrite the real theme with
      // the form defaults.
      setStatus('error')
    }
  }

  useEffect(() => {
    void load()
    return () => { if (run.current) run.current.stopped = true }
  }, [spaceId])

  const applyPreset = (p: ThemePreset) => {
    setPrimary(p.primary)
    setAccent(p.accent)
    setTint(p.surface ?? '')
  }

  const save = async () => {
    setSaving(true)
    try {
      await api.put(`/api/spaces/${spaceId}/theme`, {
        primary_color:   primary,
        accent_color:    accent,
        background_tint: tint || null,
        mode_override:   mode === 'inherit' ? null : mode,
        font_family:     font,
        post_layout:     layout,
      })
      showToast(t('theme.saved'), 'success')
      onSaved?.()
    } catch (err: unknown) {
      showToast(
        t('theme.save_failed', { error: String((err as Error)?.message ?? err) }),
        'error',
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <div class="sh-theme-studio sh-space-theme-studio">
      <h3 style={{ margin: 0 }}>{t('theme.space.title')}</h3>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
        {t('theme.space.intro')}
      </p>

      {status === 'loading' && <Spinner />}

      {status === 'error' && (
        <div class="sh-theme-studio-error" role="alert">
          <p style={{ margin: 0 }}>{t('theme.load_failed')}</p>
          <Button variant="secondary" onClick={() => void load()}>
            {t('common.try_again')}
          </Button>
        </div>
      )}

      {status === 'ready' && (
        <>
          <ThemePresetRow onApply={applyPreset} />

          <div class="sh-theme-studio-grid">
            <label>
              {t('theme.primary')}
              <input type="color" value={primary}
                     onInput={(e) => setPrimary((e.target as HTMLInputElement).value)} />
            </label>
            <label>
              {t('theme.accent')}
              <input type="color" value={accent}
                     onInput={(e) => setAccent((e.target as HTMLInputElement).value)} />
            </label>
            <label>
              {t('theme.background_tint')}
              <input type="color"
                     value={tint || '#ffffff'}
                     onInput={(e) => setTint((e.target as HTMLInputElement).value)} />
              {tint && (
                <button type="button" class="sh-link" onClick={() => setTint('')}>
                  {t('theme.clear')}
                </button>
              )}
            </label>
            <label>
              {t('theme.mode')}
              <select value={mode}
                      onChange={(e) =>
                        setMode((e.target as HTMLSelectElement).value as ModeChoice)}>
                <option value="inherit">{t('theme.mode.inherit')}</option>
                <option value="light">{t('theme.mode.light')}</option>
                <option value="dark">{t('theme.mode.dark')}</option>
              </select>
            </label>
          </div>

          <div class="sh-theme-studio-choices">
            <ThemeFontPicker
              name="space-theme-font" value={font} onChange={setFont}
              system={{
                // ``system`` = no space override → the household font
                // (useSpaceTheme leaves ``--hh-font`` in charge).
                title: t('theme.font.inherit'),
                hint: t('theme.font.inherit_hint'),
                stack: householdFontStack.value,
              }}
            />
            <RadioCardGroup
              legend={t('theme.layout')}
              name="space-theme-layout"
              value={layout}
              options={layoutOptions()}
              onChange={v => setLayout(v as LayoutId)}
            />
          </div>

          <div class="sh-theme-studio-preview"
               data-post-layout={layout}
               aria-label={t('theme.preview')}
               style={{
                 '--preview-primary': primary,
                 '--preview-accent': accent,
                 '--preview-tint': tint || 'transparent',
                 fontFamily: font === 'system' ? householdFontStack.value : FONT_STACKS[font],
               } as Record<string, string>}>
            {[0, 1].map(i => (
              <div key={i} class="sh-theme-preview-card">
                <div class="sh-theme-preview-header">
                  <span class="sh-theme-preview-dot" /> <strong>{t('theme.preview')}</strong>
                </div>
                <p>{t('theme.space.preview_text')}</p>
                <div class="sh-theme-preview-chip">👍 3</div>
              </div>
            ))}
          </div>

          <div class="sh-form-actions">
            <Button onClick={save} loading={saving}>{t('theme.save')}</Button>
          </div>
        </>
      )}
    </div>
  )
}
