import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, renderHook, waitFor } from '@testing-library/preact'

const api = vi.hoisted(() => ({ get: vi.fn(), upload: vi.fn(), delete: vi.fn(), put: vi.fn(), post: vi.fn(), patch: vi.fn() }))
const { toast, confirm } = vi.hoisted(() => ({ toast: vi.fn(), confirm: vi.fn() }))
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))

const haLogoFile = vi.hoisted(() => vi.fn())
vi.mock('@/features/spaces/haLogo', () => ({ haLogoUrl: '/assets/ha-logo.png', haLogoFile }))

import { BasicsSection } from './BasicsSection'

const haFile = new File(['png'], 'home-assistant.png', { type: 'image/png' })
import { basicsOf, useBasicsForm } from './useBasicsForm'

const base: { id: string; name: string; emoji: string; description: string; about_markdown: string; cover_url: string | null; icon_url: string | null } = { id: 's1', name: 'Garden', emoji: '🥕', description: 'Veg', about_markdown: '', cover_url: null, icon_url: null }

function form(space = base) {
  return renderHook(() => useBasicsForm(basicsOf(space))).result.current
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  api.get.mockResolvedValue({ links: [] })
  toast.mockReset(); confirm.mockReset()
})

describe('BasicsSection', () => {
  it('previews the uploaded picture when there is one, the emoji otherwise', () => {
    const plain = render(<BasicsSection space={base} form={form()} isRemoteSpace onUpdate={vi.fn()} />)
    expect(plain.getByTestId('basics-icon').textContent).toBe('🥕')
    plain.unmount()
    const withIcon = { ...base, icon_url: 'api/spaces/s1/icon?v=1' }
    const pic = render(<BasicsSection space={withIcon} form={form(withIcon)} isRemoteSpace onUpdate={vi.fn()} />)
    const icon = pic.getByTestId('basics-icon')
    expect(icon.textContent).toBe('')
    expect(icon.getAttribute('style')).toContain('api/spaces/s1/icon?v=1')
  })

  it('a picture saves at once (not through the save bar)', async () => {
    api.upload.mockResolvedValue({ icon_url: 'x' })
    const onUpdate = vi.fn()
    const view = render(<BasicsSection space={base} form={form()} isRemoteSpace onUpdate={onUpdate} />)
    const input = view.container.querySelectorAll('input[type="file"]')[1] as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'i.png', { type: 'image/png' })] })
    fireEvent.change(input)
    await waitFor(() => expect(api.upload).toHaveBeenCalledWith('/api/spaces/s1/icon', expect.any(FormData)))
    expect(onUpdate).toHaveBeenCalled()
  })

  it('offers the Home Assistant logo, which uploads like a picked picture', async () => {
    haLogoFile.mockResolvedValue(haFile)
    api.upload.mockResolvedValue({ icon_url: 'x' })
    const onUpdate = vi.fn()
    const view = render(<BasicsSection space={base} form={form()} isRemoteSpace onUpdate={onUpdate} />)
    const btn = view.getByRole('button', { name: 'Use the Home Assistant logo' })
    expect(btn.querySelector('img')?.getAttribute('src')).toBe('/assets/ha-logo.png')
    expect(btn.querySelector('img')?.getAttribute('alt')).toBe('')
    fireEvent.click(btn)
    await waitFor(() => expect(api.upload).toHaveBeenCalledWith('/api/spaces/s1/icon', expect.any(FormData)))
    expect((api.upload.mock.calls[0][1] as FormData).get('file')).toBe(haFile)
    await waitFor(() => expect(onUpdate).toHaveBeenCalled())
    expect(toast).toHaveBeenCalledWith('Icon updated', 'success')
  })

  it('a failed logo upload is said, nothing else changes', async () => {
    haLogoFile.mockRejectedValue(new Error('HTTP 404'))
    const onUpdate = vi.fn()
    const view = render(<BasicsSection space={base} form={form()} isRemoteSpace onUpdate={onUpdate} />)
    fireEvent.click(view.getByRole('button', { name: 'Use the Home Assistant logo' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringContaining('HTTP 404'), 'error'))
    expect(api.upload).not.toHaveBeenCalled()
    expect(onUpdate).not.toHaveBeenCalled()
  })

  it('quick links and the theme are host-local: hidden on a space hosted elsewhere', () => {
    const remote = render(<BasicsSection space={base} form={form()} isRemoteSpace onUpdate={vi.fn()} />)
    expect(remote.queryByText('Space theme')).toBeNull()
    remote.unmount()
    const here = render(<BasicsSection space={base} form={form()} isRemoteSpace={false} onUpdate={vi.fn()} />)
    expect(here.getByText('Space theme')).toBeTruthy()
  })

  it('About has a preview', () => {
    const f = form({ ...base, about_markdown: '**hi**' })
    const view = render(<BasicsSection space={base} form={f} isRemoteSpace onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('button', { name: 'Preview' }))
    expect(view.container.querySelector('.sh-about-editor-preview strong')?.textContent).toBe('hi')
  })
})
