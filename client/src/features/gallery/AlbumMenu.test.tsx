import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiPatch = vi.fn(async () => null)
const apiDelete = vi.fn(async () => null)
const apiPost = vi.fn(async () => ({ restored: 2 }))
vi.mock('@/api', () => ({
  api: {
    patch: (...a: unknown[]) => apiPatch(...(a as [])),
    delete: (...a: unknown[]) => apiDelete(...(a as [])),
    post: (...a: unknown[]) => apiPost(...(a as [])),
  },
}))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a), dismissToast: vi.fn() }))
const confirmDialog = vi.fn(async (_m: string, _o: { title?: string }) => true)
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: [string, { title?: string }]) => confirmDialog(...a) }))

import { AlbumMenu, type MenuAlbum } from './AlbumMenu'

const album: MenuAlbum = { id: 'al-1', space_id: null, name: 'Sommer', item_count: 2, cover_item_id: null }
const covers = [
  { id: 'it-1', thumbnail_url: 'api/media/t1.webp', item_type: 'photo' },
  { id: 'it-2', thumbnail_url: 'api/media/t2.webp', item_type: 'photo' },
]

beforeEach(() => { apiPatch.mockClear(); apiDelete.mockClear(); apiPost.mockClear(); showToast.mockClear() })
afterEach(() => cleanup())

function mount(a: MenuAlbum = album) {
  const props = { onChanged: vi.fn(), onDeleted: vi.fn(), onRestored: vi.fn() }
  const view = render(<AlbumMenu album={a} covers={covers} {...props} />)
  fireEvent.click(view.getByRole('button', { name: 'Options for Sommer' }))
  return { view, props }
}

describe('AlbumMenu', () => {
  it('renames and describes the album', async () => {
    const { view, props } = mount()
    fireEvent.click(view.getByRole('menuitem', { name: 'Rename and describe' }))
    const name = view.getByRole('textbox', { name: 'Name' }) as HTMLInputElement
    fireEvent.input(name, { target: { value: 'Sommer 2026' } })
    fireEvent.click(view.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/gallery/albums/al-1',
      { name: 'Sommer 2026', description: '' }))
    expect(props.onChanged).toHaveBeenCalled()
  })

  it('chooses a cover from the album', async () => {
    const { view } = mount()
    fireEvent.click(view.getByRole('menuitem', { name: 'Choose cover' }))
    fireEvent.click(view.getByRole('radio', { name: 'Cover 2' }))
    fireEvent.click(view.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/gallery/albums/al-1', { cover_item_id: 'it-2' }))
  })

  it('household: delete sends the album to the trash, Undo brings it back', async () => {
    const { view, props } = mount()
    fireEvent.click(view.getByRole('menuitem', { name: 'Delete album' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/gallery/albums/al-1'))
    expect(confirmDialog.mock.calls[0][0]).toContain('go to the trash with it')
    expect(props.onDeleted).toHaveBeenCalled()
    const [msg, , opts] = showToast.mock.calls[0]
    expect(msg).toBe('“Sommer” moved to the trash')
    opts.action.onClick()
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/gallery/albums/al-1/restore'))
    await waitFor(() => expect(props.onRestored).toHaveBeenCalledWith('al-1'))
  })

  it('space: the album delete waits for its Undo before it is sent', async () => {
    const { view, props } = mount({ ...album, space_id: 'sp-1' })
    fireEvent.click(view.getByRole('menuitem', { name: 'Delete album' }))
    await waitFor(() => expect(props.onDeleted).toHaveBeenCalled())
    expect(apiDelete).not.toHaveBeenCalled()
    const [, , opts] = showToast.mock.calls[0]
    opts.onExpire()
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/gallery/albums/al-1', { keepalive: undefined }))
  })
})
