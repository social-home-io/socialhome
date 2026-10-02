import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const get = vi.fn()
const put = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => get(...a),
    put: (...a: unknown[]) => put(...a),
  },
}))
const showToast = vi.fn()
vi.mock('./Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))

import { SpaceThemeStudio } from './SpaceThemeStudio'
import { householdFont } from '@/store/householdTheme'

const STORED = {
  primary_color: '#3366ff',
  accent_color: '#c8902f',
  background_tint: null,
  mode_override: null,
  font_family: 'system',
  post_layout: 'card',
  is_default: false,
}

function radios(container: ParentNode, name: string): HTMLInputElement[] {
  return [...container.querySelectorAll<HTMLInputElement>(`input[type="radio"][name="${name}"]`)]
}

function checked(container: ParentNode, name: string): string | undefined {
  return radios(container, name).find(r => r.checked)?.value
}

async function mounted(theme: unknown) {
  get.mockResolvedValue(theme)
  const view = render(<SpaceThemeStudio spaceId="sp-1" />)
  await waitFor(() => expect(view.getByText('Save theme')).toBeTruthy())
  return view
}

beforeEach(() => {
  householdFont.value = 'system'
  get.mockReset()
  put.mockReset()
  showToast.mockReset()
  put.mockResolvedValue({})
})

describe('SpaceThemeStudio', () => {
  it('offers exactly the server font ids and layout ids', async () => {
    const { container } = await mounted(STORED)
    expect(radios(container, 'space-theme-font').map(r => r.value))
      .toEqual(['system', 'serif', 'rounded', 'mono'])
    expect(radios(container, 'space-theme-layout').map(r => r.value))
      .toEqual(['card', 'compact', 'magazine'])
  })

  it('previews each font option in its real stack', async () => {
    const { getByText } = await mounted(STORED)
    expect(getByText('Serif').style.fontFamily).toContain('Georgia')
    expect(getByText('Mono').style.fontFamily).toContain('monospace')
  })

  it('sends ids — never a CSS stack — for font and layout', async () => {
    const { container, getByText } = await mounted(STORED)
    fireEvent.click(radios(container, 'space-theme-font').find(r => r.value === 'serif')!)
    fireEvent.click(radios(container, 'space-theme-layout').find(r => r.value === 'magazine')!)
    fireEvent.click(getByText('Save theme'))
    await waitFor(() => expect(put).toHaveBeenCalled())
    const [path, body] = put.mock.calls[0]
    expect(path).toBe('/api/spaces/sp-1/theme')
    expect(body.font_family).toBe('serif')
    expect(body.post_layout).toBe('magazine')
    expect(showToast).toHaveBeenCalledWith('Theme saved', 'success')
  })

  it('selects the stored ids', async () => {
    const { container } = await mounted({ ...STORED, font_family: 'mono', post_layout: 'compact' })
    expect(checked(container, 'space-theme-font')).toBe('mono')
    expect(checked(container, 'space-theme-layout')).toBe('compact')
  })

  it('falls back to System / Card for a value outside the schema', async () => {
    // The CHECK constraint forbids these on disk; the studio still must not
    // echo one back (the old studio's CSS stack / "spacious" 422'd).
    const { container, getByText } = await mounted({
      ...STORED, font_family: 'Inter, system-ui, sans-serif', post_layout: 'spacious',
    })
    expect(checked(container, 'space-theme-font')).toBe('system')
    expect(checked(container, 'space-theme-layout')).toBe('card')
    fireEvent.click(getByText('Save theme'))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0][1].font_family).toBe('system')
    expect(put.mock.calls[0][1].post_layout).toBe('card')
  })

  it('starts from the brand defaults when the space has no theme yet', async () => {
    // GET falls back to the HOUSEHOLD row (is_default) — its colours and
    // font are not space overrides, so saving must not pin them here.
    const { container, getByText } = await mounted({
      primary_color: '#112233', accent_color: '#445566', surface_color: '#eeeeee', mode: 'dark',
      font_family: 'serif', density: 'comfortable', corner_radius: 12, is_default: true,
    })
    expect(checked(container, 'space-theme-font')).toBe('system')
    expect(checked(container, 'space-theme-layout')).toBe('card')
    fireEvent.click(getByText('Save theme'))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0][1]).toEqual({
      primary_color: '#D2542A', accent_color: '#C8902F', background_tint: null,
      mode_override: null, font_family: 'system', post_layout: 'card',
    })
  })

  it('labels "system" as Same as household and previews the household font', async () => {
    // At space scope ``system`` = no override → the household font
    // (useSpaceTheme leaves --hh-font in charge). Household serif + space
    // System must preview serif, not the device font.
    householdFont.value = 'serif'
    const { container, getByText } = await mounted(STORED)
    expect(checked(container, 'space-theme-font')).toBe('system')
    const title = getByText('Same as household')
    expect(title.style.fontFamily).toContain('Georgia')
    expect(getByText("Uses your household's font.")).toBeTruthy()
    const preview = container.querySelector<HTMLElement>('.sh-theme-studio-preview')!
    expect(preview.style.fontFamily).toContain('Georgia')
    // The id on the wire stays ``system``.
    fireEvent.click(getByText('Save theme'))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0][1].font_family).toBe('system')
  })

  it('ignores a slow response for a space the studio has moved away from', async () => {
    let resolveA: (v: unknown) => void = () => {}
    get.mockImplementation((path: string) => path.includes('sp-a')
      ? new Promise(r => { resolveA = r })
      : Promise.resolve({ ...STORED, font_family: 'mono' }))
    const view = render(<SpaceThemeStudio spaceId="sp-a" />)
    view.rerender(<SpaceThemeStudio spaceId="sp-b" />)
    await waitFor(() => expect(view.getByText('Save theme')).toBeTruthy())
    expect(checked(view.container, 'space-theme-font')).toBe('mono')
    resolveA({ ...STORED, font_family: 'serif' })
    await new Promise(r => setTimeout(r, 0))
    expect(checked(view.container, 'space-theme-font')).toBe('mono')
  })

  it('previews the chosen layout and font in the preview pane', async () => {
    const { container } = await mounted(STORED)
    fireEvent.click(radios(container, 'space-theme-layout').find(r => r.value === 'compact')!)
    fireEvent.click(radios(container, 'space-theme-font').find(r => r.value === 'rounded')!)
    const preview = container.querySelector<HTMLElement>('.sh-theme-studio-preview')!
    expect(preview.getAttribute('data-post-layout')).toBe('compact')
    expect(preview.style.fontFamily).toContain('Quicksand')
  })

  it("shows the server's reason when a save is refused", async () => {
    put.mockRejectedValue(new Error('font_family must be one of: mono, rounded, serif, system'))
    const { getByText } = await mounted(STORED)
    fireEvent.click(getByText('Save theme'))
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    expect(showToast).toHaveBeenCalledWith(
      'Save failed: font_family must be one of: mono, rounded, serif, system', 'error',
    )
  })

  it('does not offer a save over a theme it could not load, and can retry', async () => {
    get.mockRejectedValueOnce(new Error('offline'))
    const { queryByText, getByText } = render(<SpaceThemeStudio spaceId="sp-1" />)
    await waitFor(() => expect(getByText('Try again')).toBeTruthy())
    expect(queryByText('Save theme')).toBeNull()
    get.mockResolvedValue(STORED)
    fireEvent.click(getByText('Try again'))
    await waitFor(() => expect(getByText('Save theme')).toBeTruthy())
  })
})
