/**
 * PagesView on both scopes: the space scope never calls the household
 * lock / revert routes, reads its own versions route, routes writes
 * through ``contentWrite`` (a 202 toasts and opens nothing), turns a 409
 * into the side-by-side conflict view, and — for an edit held for review —
 * never autosaves. A read-only scope offers no create / edit / delete.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup, act } from '@testing-library/preact'
import type { Page } from '@/types'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    patch: (...a: unknown[]) => apiPatch(...a),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
}))

const toast = vi.hoisted(() => ({ show: vi.fn((..._a: unknown[]) => 1) }))
vi.mock('@/components/Toast', async (orig) => ({
  ...(await orig<object>()),
  showToast: toast.show,
}))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn().mockResolvedValue(true) }))
vi.mock('@/store/moderationMine', async (orig) => ({
  ...(await orig<object>()),
  refreshModerationMine: vi.fn(),
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

import { moderationMine } from '@/store/moderationMine'
import type { ModerationItem } from '@/features/spaces/moderationItems'
import { PagesView } from './PagesView'
import { householdPageScope, spacePageScope, type PageScope } from './scope'

function page(over: Partial<Page> = {}): Page {
  return {
    id: 'p1', title: 'Plan', content: 'hello', created_by: 'u2',
    created_at: '2026-01-01T00:00:00+00:00', updated_at: '2026-01-01T00:00:00+00:00',
    last_editor_user_id: 'u2', last_edited_at: '2026-01-01T00:00:00+00:00',
    space_id: 's1', cover_image_url: null, locked_by: null, locked_at: null,
    lock_expires_at: null, ...over,
  }
}

const BASE = '/api/spaces/s1/pages'

function space(over: Partial<Parameters<typeof spacePageScope>[0]> = {}): PageScope {
  return spacePageScope({
    spaceId: 's1', role: 'member', level: 'open', writable: true, archived: false, ...over,
  })
}

function wire(rows: Page[] = [page()]) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === BASE || url === '/api/pages') return rows
    if (url.endsWith('/versions')) {
      return [{ id: 'v1', page_id: 'p1', version: 1, title: 'Plan', content: 'old', edited_by: 'u2', edited_at: '2026-01-01T00:00:00+00:00', space_id: 's1', cover_image_url: null }]
    }
    const found = rows.find(p => url.endsWith(`/${p.id}`))
    if (found) return found
    return null
  })
}

beforeEach(() => {
  apiGet.mockReset(); apiPost.mockReset(); apiPatch.mockReset(); apiDelete.mockReset()
  toast.show.mockClear()
  moderationMine.value = {}
})
afterEach(() => { cleanup(); vi.useRealTimers() })

function mineItem(over: Partial<ModerationItem> = {}): ModerationItem {
  return {
    id: 'm1', space_id: 's1', feature: 'pages', action: 'edit', target_id: 'p1',
    submitted_by: 'u1', submitted_at: '2026-01-01T00:00:00+00:00',
    expires_at: '2099-01-01T00:00:00+00:00', status: 'pending', ...over,
  }
}

async function openPage(r: ReturnType<typeof render>, title = 'Plan') {
  fireEvent.click(await waitFor(() => r.getByRole('button', { name: new RegExp(title) })))
  await waitFor(() => r.getByRole('heading', { name: title }))
}

describe('PagesView — space scope', () => {
  it('lists, opens and edits without touching lock routes; history reads the space route', async () => {
    wire()
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'History' }))
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith(`${BASE}/p1/versions`))
    expect(r.getByText(/history is read-only/)).toBeTruthy()
    fireEvent.click(r.getByRole('button', { name: 'Close history' }))
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    await waitFor(() => r.getByRole('textbox', { name: 'Markdown source' }))
    const urls = [...apiGet.mock.calls, ...apiPost.mock.calls, ...apiDelete.mock.calls]
      .map(c => String(c[0]))
    expect(urls.some(u => u.includes('/lock'))).toBe(false)
  })

  it('the mobile Edit / Preview tabs name the pane they control', async () => {
    wire()
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    const tab = await waitFor(() => r.getByRole('tab', { name: 'Edit' }))
    expect(tab.getAttribute('aria-pressed')).toBeNull()
    expect(tab.getAttribute('aria-selected')).toBe('true')
    const source = r.getByRole('textbox', { name: 'Markdown source' })
    expect(tab.getAttribute('aria-controls')).toBe(source.id)
    const preview = r.getByRole('tab', { name: 'Preview' })
    expect(document.getElementById(preview.getAttribute('aria-controls')!)).toBeTruthy()
  })

  it('saves title + body in one PATCH with the base version', async () => {
    wire()
    apiPatch.mockImplementation(async (_u: string, body: Record<string, unknown>) =>
      page({ content: String(body.content), title: String(body.title), updated_at: '2026-01-02T00:00:00+00:00' }))
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    fireEvent.input(r.getByRole('textbox', { name: 'Markdown source' }), { target: { value: 'new body' } })
    fireEvent.input(r.getByRole('textbox', { name: 'Title' }), { target: { value: 'New title' } })
    fireEvent.click(r.getByRole('button', { name: 'Save & close' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(`${BASE}/p1`, {
      content: 'new body', title: 'New title', base_updated_at: '2026-01-01T00:00:00+00:00',
    }))
    await waitFor(() => r.getByRole('heading', { name: 'New title' }))
  })

  it('a 409 opens the conflict view; keep mine saves over their version', async () => {
    const theirs = page({ content: 'their body', updated_at: '2026-01-03T00:00:00+00:00', last_editor_user_id: 'u3' })
    let first = true
    apiGet.mockImplementation(async (url: string) => {
      if (url === BASE) return [page()]
      if (url === `${BASE}/p1`) {
        if (first) { first = false; return page() }
        return theirs
      }
      return []
    })
    apiPatch.mockRejectedValueOnce(Object.assign(new Error('API 409'), { status: 409 }))
    apiPatch.mockResolvedValueOnce(page({ content: 'mine', updated_at: '2026-01-04T00:00:00+00:00' }))
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    fireEvent.input(r.getByRole('textbox', { name: 'Markdown source' }), { target: { value: 'mine' } })
    fireEvent.click(r.getByRole('button', { name: 'Save & close' }))
    await waitFor(() => r.getByRole('button', { name: 'Keep mine' }))
    expect(r.getByText('their body')).toBeTruthy()
    fireEvent.click(r.getByRole('button', { name: 'Keep mine' }))
    await waitFor(() => expect(apiPatch).toHaveBeenLastCalledWith(`${BASE}/p1`, {
      content: 'mine', base_updated_at: '2026-01-03T00:00:00+00:00',
    }))
  })

  it('a reviewed create collects the body and a 202 opens nothing', async () => {
    wire([])
    apiPost.mockResolvedValue({ queued: true, item_id: 'q1', feature: 'pages', action: 'create' })
    const r = render(<PagesView scope={space({ level: 'moderated' })} />)
    fireEvent.click(await waitFor(() => r.getByRole('button', { name: '+ New page' })))
    expect(r.getByText(/moderator looks at your new page/)).toBeTruthy()
    fireEvent.input(r.getByRole('textbox', { name: 'Title' }), { target: { value: 'Rules' } })
    fireEvent.input(r.getByRole('textbox', { name: 'Content' }), { target: { value: 'Be kind' } })
    fireEvent.click(r.getByRole('button', { name: 'Submit for review' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(BASE, { title: 'Rules', content: 'Be kind' }))
    await waitFor(() => expect(toast.show).toHaveBeenCalledWith(
      'Submitted for review — a moderator will look at it', 'info'))
    expect(r.queryByRole('textbox', { name: 'Markdown source' })).toBeNull()
  })

  it('an edit of somebody else\'s page held for review never autosaves', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    wire()
    apiPatch.mockResolvedValue({ queued: true, item_id: 'q2', feature: 'pages', action: 'edit' })
    const r = render(<PagesView scope={space({ level: 'moderated' })} />)
    await openPage(r)
    expect(r.getAllByText(/your edit goes to a moderator/).length).toBeGreaterThan(0)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    fireEvent.input(r.getByRole('textbox', { name: 'Markdown source' }), { target: { value: 'suggestion' } })
    await act(async () => { vi.advanceTimersByTime(5000) })
    expect(apiPatch).not.toHaveBeenCalled()
    fireEvent.click(r.getByRole('button', { name: 'Submit for review' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledTimes(1))
    // Queued: back to the unchanged page.
    await waitFor(() => r.getByRole('heading', { name: 'Plan' }))
    expect(r.getByText('hello')).toBeTruthy()
  })

  it('a moderator\'s edits under review save directly (autosave on)', async () => {
    const scope = space({ level: 'moderated', role: 'moderator' })
    expect(scope.reviewed(page())).toBe(false)
    expect(scope.reviewedCreate).toBe(false)
    // A member's own page isn't reviewed either.
    expect(space({ level: 'moderated' }).reviewed(page({ created_by: 'u1' }))).toBe(false)
  })

  it('a queued delete keeps the page', async () => {
    wire()
    apiDelete.mockResolvedValue({ queued: true, item_id: 'q3', feature: 'pages', action: 'delete' })
    const r = render(<PagesView scope={space({ level: 'moderated' })} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith(`${BASE}/p1`))
    expect(r.getByRole('heading', { name: 'Plan' })).toBeTruthy()
  })

  it('the viewer says when the viewer\'s own edit of this page waits for review', async () => {
    wire([page(), page({ id: 'p2', title: 'Other' })])
    moderationMine.value = { s1: [
      mineItem(),
      // Not shown: another page's edit, a decided one, a new-page create.
      mineItem({ id: 'm2', target_id: 'p2', action: 'delete' }),
      mineItem({ id: 'm3', status: 'rejected', action: 'delete' }),
      mineItem({ id: 'm4', action: 'create', target_id: null }),
    ] }
    const r = render(<PagesView scope={space({ level: 'moderated' })} />)
    await openPage(r)
    const note = r.getByTestId('page-pending-review')
    expect(note.getAttribute('role')).toBe('status')
    expect(note.textContent).toContain('Your edit to this page is waiting for review.')
    expect(note.textContent).not.toContain('delete')
    // The moderator decides: the store refreshes and the notice goes.
    act(() => { moderationMine.value = { s1: [mineItem({ status: 'approved' })] } })
    expect(r.queryByTestId('page-pending-review')).toBeNull()
    // A pending delete reads as one.
    act(() => { moderationMine.value = { s1: [mineItem({ id: 'm5', action: 'delete' })] } })
    expect(r.getByTestId('page-pending-review').textContent)
      .toContain('Your request to delete this page is waiting for review.')
  })

  it('no pending notice on a page the viewer has nothing pending on', async () => {
    wire()
    moderationMine.value = { s1: [mineItem({ target_id: 'p9' })] }
    const r = render(<PagesView scope={space({ level: 'moderated' })} />)
    await openPage(r)
    expect(r.queryByTestId('page-pending-review')).toBeNull()
  })

  it('read-only (admin-only for a member, or archived): no create / edit / delete', async () => {
    wire()
    const r = render(<PagesView scope={space({ writable: false })} header={<p>note</p>} />)
    await waitFor(() => r.getByText('note'))
    expect(r.queryByRole('button', { name: '+ New page' })).toBeNull()
    await openPage(r)
    expect(r.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(r.queryByRole('button', { name: 'Delete' })).toBeNull()
    expect(r.getByRole('button', { name: 'History' })).toBeTruthy()
    cleanup()
    expect(space({ archived: true }).canWrite).toBe(false)
  })

  it('a failed list load offers Retry', async () => {
    apiGet.mockRejectedValueOnce(new Error('boom'))
    const r = render(<PagesView scope={space()} />)
    await waitFor(() => r.getByText('Pages couldn’t be loaded.'))
    wire()
    fireEvent.click(r.getByRole('button', { name: 'Retry' }))
    await waitFor(() => r.getByRole('button', { name: /Plan/ }))
  })
})

describe('PagesView — save ordering and unmount', () => {
  it('Save & close waits for an in-flight autosave and bases on its answer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    wire()
    let release: (p: Page) => void = () => {}
    apiPatch.mockImplementationOnce(() => new Promise<Page>(res => { release = res }))
    apiPatch.mockImplementationOnce(async (_u: string, body: Record<string, unknown>) =>
      page({ content: String(body.content), updated_at: '2026-01-03T00:00:00+00:00' }))
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    const box = r.getByRole('textbox', { name: 'Markdown source' })
    fireEvent.input(box, { target: { value: 'first' } })
    await act(async () => { vi.advanceTimersByTime(2000) })   // autosave fires, held
    expect(apiPatch).toHaveBeenCalledTimes(1)
    fireEvent.input(box, { target: { value: 'second' } })
    fireEvent.click(r.getByRole('button', { name: 'Save & close' }))
    await act(async () => { await Promise.resolve() })
    expect(apiPatch).toHaveBeenCalledTimes(1)               // waits
    await act(async () => {
      release(page({ content: 'first', updated_at: '2026-01-02T00:00:00+00:00' }))
    })
    await waitFor(() => expect(apiPatch).toHaveBeenCalledTimes(2))
    expect(apiPatch.mock.calls[0][1]).toEqual({ content: 'first', base_updated_at: '2026-01-01T00:00:00+00:00' })
    expect(apiPatch.mock.calls[1][1]).toEqual({ content: 'second', base_updated_at: '2026-01-02T00:00:00+00:00' })
    await waitFor(() => r.getByRole('heading', { name: 'Plan' }))
    expect(r.queryByRole('button', { name: 'Keep mine' })).toBeNull()
  })

  it('unmounting with unsaved changes flushes them with keepalive', async () => {
    wire()
    apiPatch.mockResolvedValue(page({ content: 'unsaved' }))
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    fireEvent.input(r.getByRole('textbox', { name: 'Markdown source' }), { target: { value: 'unsaved' } })
    r.unmount()
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(
      `${BASE}/p1`,
      { content: 'unsaved', base_updated_at: '2026-01-01T00:00:00+00:00' },
      { keepalive: true },
    ))
  })

  it('an unsubmitted reviewed edit is not sent on unmount', async () => {
    wire()
    const r = render(<PagesView scope={space({ level: 'moderated' })} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    fireEvent.input(r.getByRole('textbox', { name: 'Markdown source' }), { target: { value: 'draft' } })
    r.unmount()
    await new Promise(res => setTimeout(res, 20))
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('opening pages quickly shows the last one asked for', async () => {
    const a = page({ id: 'pa', title: 'Alpha' })
    const b = page({ id: 'pb', title: 'Beta' })
    let releaseA: (p: Page) => void = () => {}
    apiGet.mockImplementation(async (url: string) => {
      if (url === BASE) return [a, b]
      if (url === `${BASE}/pa`) return new Promise<Page>(res => { releaseA = res })
      if (url === `${BASE}/pb`) return b
      return []
    })
    const r = render(<PagesView scope={space()} />)
    fireEvent.click(await waitFor(() => r.getByRole('button', { name: /Alpha/ })))
    fireEvent.click(r.getByRole('button', { name: /Beta/ }))
    await waitFor(() => r.getByRole('heading', { name: 'Beta' }))
    await act(async () => { releaseA(a); await new Promise(res => setTimeout(res, 20)) })
    expect(r.getByRole('heading', { name: 'Beta' })).toBeTruthy()
    expect(r.queryByRole('heading', { name: 'Alpha' })).toBeNull()
  })
})

describe('PagesView — household scope', () => {
  it('takes the edit lock and offers restore to admins', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/pages') return [page({ space_id: null })]
      if (url === '/api/pages/p1') return page({ space_id: null })
      if (url === '/api/pages/p1/lock') return null
      if (url === '/api/pages/p1/versions') {
        return [{ id: 'v1', page_id: 'p1', version: 1, title: 'Plan', content: 'old', edited_by: 'u2', edited_at: '2026-01-01T00:00:00+00:00', space_id: null, cover_image_url: null }]
      }
      return []
    })
    apiPost.mockResolvedValue({})
    apiDelete.mockResolvedValue(null)
    const r = render(<PagesView scope={householdPageScope()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'History' }))
    await waitFor(() => r.getByRole('button', { name: 'Restore this version' }))
    fireEvent.click(r.getByRole('button', { name: 'Close history' }))
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/pages/p1/lock', {}))
    fireEvent.click(r.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/pages/p1/lock'))
  })

  it('releases the edit lock when the editor unmounts', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/pages') return [page({ space_id: null })]
      if (url === '/api/pages/p1') return page({ space_id: null })
      return null
    })
    apiPost.mockResolvedValue({})
    apiDelete.mockResolvedValue(null)
    const r = render(<PagesView scope={householdPageScope()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/pages/p1/lock', {}))
    r.unmount()
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/pages/p1/lock', { keepalive: true }))
  })
})
