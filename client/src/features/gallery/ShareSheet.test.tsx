import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const targets = {
  feed: true,
  spaces: [
    {
      id: 'sp-oma', name: 'Grandparents', emoji: '👵', space_type: 'private', households: 3,
      post: true, post_review: false, album: true,
    },
    {
      id: 'sp-club', name: 'U11 football', emoji: null, space_type: 'private', households: 14,
      post: true, post_review: true, album: false,
    },
  ],
  chats: [{ id: 'c-1', name: 'Oma Gisela', group: false, households: 2 }],
  limits: { max_items: 50, post_items: 5, chat_items: 5, video_seconds: 60 },
}
const apiGet = vi.fn(async (url: string) => {
  if (url === '/api/gallery/share/targets') return targets
  if (url.startsWith('/api/spaces/sp-oma/gallery/albums')) {
    return [{ id: 'sys', name: 'Posts', is_system: true }, { id: 'al-reunion', name: 'Family reunion' }]
  }
  throw new Error(url)
})
const apiPost = vi.fn(async () => ({ shared: 2, skipped: [], trimming: 1, queued: false }))
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...(a as [string])),
    post: (...a: unknown[]) => apiPost(...(a as [])),
  },
  ApiError: class ApiError extends Error { status = 0 },
}))
vi.mock('@/store/auth', async () => {
  const { signal } = await import('@preact/signals')
  return { currentUser: signal({ user_id: 'u-me' }) }
})

import { ShareSheet } from './ShareSheet'
import type { ShareItem } from './gallerySharing'

const items: ShareItem[] = [
  { id: 'p1', item_type: 'photo', uploaded_by: 'u-lena', thumbnail_url: 'api/media/t1.webp' },
  {
    id: 'v1', item_type: 'video', uploaded_by: 'u-me', thumbnail_url: 'api/media/t2.webp',
    duration_s: 250, caption: 'Birthday',
  },
]

beforeEach(() => { apiGet.mockClear(); apiPost.mockClear() })
afterEach(() => cleanup())

function mount(list: ShareItem[] = items) {
  const props = { onClose: vi.fn(), onShared: vi.fn() }
  const view = render(
    <ShareSheet items={list} nameOf={(u) => (u === 'u-lena' ? 'Lena' : '')} {...props} />,
  )
  return { view, props }
}

describe('ShareSheet', () => {
  it('lists feed, spaces and chats; the audience shows before sending', async () => {
    const { view } = mount()
    await waitFor(() => view.getByRole('radio', { name: /Household feed/ }))
    expect(view.getByRole('dialog', { name: 'Share 2 items' })).toBeTruthy()
    expect(view.getByRole('radio', { name: /Grandparents/ })).toBeTruthy()
    expect(view.getByRole('radio', { name: /Oma Gisela/ })).toBeTruthy()
    // Nothing picked yet: the button waits.
    const share = view.getByRole('button', { name: 'Share a copy' }) as HTMLButtonElement
    expect(share.disabled).toBe(true)

    fireEvent.click(view.getByRole('radio', { name: /Grandparents/ }))
    expect(view.getByText('A copy goes to the space “Grandparents”. The 3 households in it can see it.'))
      .toBeTruthy()
    expect(view.getByText(/Deleting them here doesn't delete the copy/)).toBeTruthy()
    expect(view.getByText('Lena added one of these and will see that you shared it.')).toBeTruthy()
    // The long video: a visible choice, keep the first minute by default.
    expect(view.getByText(/“Birthday” is 4:10 long/)).toBeTruthy()
    expect((view.getByRole('radio', { name: 'Keep the first minute' }) as HTMLInputElement).checked).toBe(true)
  })

  it('shares into a space album with the chosen album and the trim choice', async () => {
    const { view, props } = mount()
    await waitFor(() => view.getByRole('radio', { name: /Grandparents/ }))
    fireEvent.click(view.getByRole('radio', { name: /Grandparents/ }))
    fireEvent.click(view.getByRole('radio', { name: 'Into the space album' }))
    await waitFor(() => view.getByRole('combobox', { name: 'Space album' }))
    // The system "Posts" album is never offered.
    expect(view.queryByRole('option', { name: 'Posts' })).toBeNull()
    fireEvent.click(view.getByRole('radio', { name: 'Leave it out' }))
    fireEvent.click(view.getByRole('button', { name: 'Share a copy' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/gallery/share', {
      item_ids: ['p1', 'v1'],
      long_videos: 'skip',
      target: 'space_album',
      space_id: 'sp-oma',
      album_id: 'al-reunion',
    }))
    await waitFor(() => expect(props.onShared).toHaveBeenCalled())
    expect(props.onClose).toHaveBeenCalled()
  })

  it('a chat gets the message with the copy', async () => {
    const { view } = mount()
    await waitFor(() => view.getByRole('radio', { name: /Oma Gisela/ }))
    fireEvent.click(view.getByRole('radio', { name: /Oma Gisela/ }))
    expect(view.getByText('A copy goes to the chat with Oma Gisela. 2 households are in it.')).toBeTruthy()
    fireEvent.input(view.getByRole('textbox', { name: 'Add a message (optional)' }), {
      target: { value: 'Happy birthday!' },
    })
    fireEvent.click(view.getByRole('button', { name: 'Share a copy' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/gallery/share', {
      item_ids: ['p1', 'v1'],
      long_videos: 'trim',
      target: 'chat',
      conversation_id: 'c-1',
      text: 'Happy birthday!',
    }))
  })

  it('a target that cannot take this many says why and cannot be picked', async () => {
    const many: ShareItem[] = Array.from({ length: 6 }, (_, i) => ({
      id: `p${i}`, item_type: 'photo' as const, uploaded_by: 'u-me', thumbnail_url: `api/media/t${i}.webp`,
    }))
    const { view } = mount(many)
    await waitFor(() => view.getByRole('radio', { name: /Household feed/ }))
    expect((view.getByRole('radio', { name: /Household feed/ }) as HTMLInputElement).disabled).toBe(true)
    expect((view.getByRole('radio', { name: /Oma Gisela/ }) as HTMLInputElement).disabled).toBe(true)
    expect((view.getByRole('radio', { name: /U11 football/ }) as HTMLInputElement).disabled).toBe(true)
    // The space with an album still takes all six — into its album.
    fireEvent.click(view.getByRole('radio', { name: /Grandparents/ }))
    await waitFor(() => view.getByRole('combobox', { name: 'Space album' }))
    expect(view.queryByRole('radio', { name: 'As a post in the feed' })).toBeTruthy()
    expect((view.getByRole('radio', { name: 'As a post in the feed' }) as HTMLInputElement).disabled).toBe(true)
  })

  it('a video still being prepared is left out and said so', async () => {
    const { view } = mount([...items, {
      id: 'v2', item_type: 'video', uploaded_by: 'u-me', thumbnail_url: '', processing: true,
    }])
    await waitFor(() => view.getByText('1 video is still being prepared and isn\'t shared.'))
  })

  it('a moderated space says the admins check the post first', async () => {
    const { view } = mount([items[0]!])
    await waitFor(() => view.getByRole('radio', { name: /U11 football/ }))
    fireEvent.click(view.getByRole('radio', { name: /U11 football/ }))
    expect(view.getByText("The space's admins check posts here first.")).toBeTruthy()
  })

  it('a failed load offers a retry', async () => {
    apiGet.mockRejectedValueOnce(new Error('offline'))
    const { view } = mount()
    await waitFor(() => view.getByText("Couldn't load where you can share."))
    fireEvent.click(view.getByRole('button', { name: 'Retry' }))
    await waitFor(() => view.getByRole('radio', { name: /Household feed/ }))
  })
})
