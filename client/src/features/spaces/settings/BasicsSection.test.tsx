import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, renderHook, waitFor } from '@testing-library/preact'

const api = vi.hoisted(() => ({ get: vi.fn(), upload: vi.fn(), delete: vi.fn(), put: vi.fn(), post: vi.fn(), patch: vi.fn() }))
const { toast, confirm } = vi.hoisted(() => ({ toast: vi.fn(), confirm: vi.fn() }))
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))

import { BasicsSection } from './BasicsSection'
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
