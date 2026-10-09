/**
 * ConversationView — "Delete for everyone" where the host offers it (a
 * space chat: ``allowDelete``, plus ``canModerate`` for its owner /
 * admins / moderators), and the live ``dm.message_deleted`` frame.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.setConfig({ testTimeout: 20_000 })
const RENDER_WAIT = 15_000

const apiGet = vi.fn()
const apiDelete = vi.fn()
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: {}, path: '/' }),
  useLocation: () => ({ url: '/', route: vi.fn() }),
}))
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    delete: (...args: unknown[]) => apiDelete(...args),
    upload: vi.fn().mockResolvedValue({}),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const wsHandlers: Record<string, Handler[]> = {}
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      ;(wsHandlers[type] ??= []).push(h)
      return () => { wsHandlers[type] = (wsHandlers[type] ?? []).filter(x => x !== h) }
    },
    send: vi.fn(),
  },
}))
function emit(type: string, data: Record<string, unknown>): void {
  for (const h of wsHandlers[type] ?? []) h({ type, data })
}

const confirmDialog = vi.fn()
vi.mock('@/components/confirm', () => ({
  confirmDialog: (...a: unknown[]) => confirmDialog(...a),
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 't' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const msgRow = (id: string, content: string, sender: string) => ({
  id, sender_user_id: sender, content, type: 'text',
  media_url: null, file_name: null, mime_type: null,
  file_size_bytes: null, reply_to_id: null,
  reactions: [{ user_id: 'u-me', emoji: '👍' }], deleted: false,
  created_at: '2026-05-17T13:00:42+00:00', edited_at: null,
})

const META = {
  type: 'group_dm' as const, name: null, managed_here: false,
  muted_until: null, notif_level: 'all' as const, unread: 0, last_read_at: null,
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiDelete.mockReset()
  apiDelete.mockResolvedValue({ ok: true })
  confirmDialog.mockReset()
  confirmDialog.mockResolvedValue(true)
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
  apiGet.mockImplementation(async (url: string) => {
    if (url.startsWith('/api/conversations/sc-1/messages')) {
      return [msgRow('m-mine', 'MINE', 'u-me'), msgRow('m-bob', 'BOB', 'u-bob')]
    }
    if (url === '/api/conversations/sc-1/members') {
      return [
        { user_id: 'u-me', username: 'me', display_name: 'Me', is_self: true },
        { user_id: 'u-bob', username: 'bob', display_name: 'Bob', is_self: false },
      ]
    }
    return {}
  })
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
})

async function renderView(props: Record<string, unknown>) {
  const tl = await import('@testing-library/preact')
  const { ConversationView } = await import('./ConversationView')
  const r = tl.render(
    <ConversationView conversationId="sc-1" embedded showHeader={false} meta={META} {...props} />,
  )
  await tl.waitFor(() => expect(r.getByText('BOB')).toBeTruthy(), { timeout: RENDER_WAIT })
  return { ...tl, ...r }
}

const bubble = (root: Element, id: string) =>
  root.querySelector<HTMLElement>(`[data-msg-id="${id}"]`)!

describe('ConversationView — delete', () => {
  it('no delete at all by default (a DM thread)', async () => {
    const r = await renderView({})
    expect(r.queryByRole('button', { name: 'Delete your message' })).toBeNull()
    expect(r.queryByRole('button', { name: "Delete Bob's message" })).toBeNull()
  })

  it('allowDelete: own messages only, for a plain member', async () => {
    const r = await renderView({ allowDelete: true })
    expect(r.getByRole('button', { name: 'Delete your message' })).toBeTruthy()
    expect(r.queryByRole('button', { name: "Delete Bob's message" })).toBeNull()
  })

  it('canModerate deletes somebody else\'s message after a confirm', async () => {
    const r = await renderView({ allowDelete: true, canModerate: true })
    r.fireEvent.click(r.getByRole('button', { name: "Delete Bob's message" }))
    await r.waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/conversations/sc-1/messages/m-bob'))
    expect(confirmDialog).toHaveBeenCalledWith(
      expect.stringContaining('Everyone in the chat'), expect.objectContaining({ destructive: true }),
    )
    await r.waitFor(() => expect(r.queryByText('BOB')).toBeNull())
    expect(bubble(r.container, 'm-bob').className).toContain('sh-message--deleted')
    expect(r.getByText('MINE')).toBeTruthy()
  })

  it('a cancelled confirm deletes nothing', async () => {
    confirmDialog.mockResolvedValue(false)
    const r = await renderView({ allowDelete: true })
    r.fireEvent.click(r.getByRole('button', { name: 'Delete your message' }))
    await new Promise(res => setTimeout(res, 20))
    expect(apiDelete).not.toHaveBeenCalled()
    expect(r.getByText('MINE')).toBeTruthy()
  })

  it('a refused delete keeps the message', async () => {
    apiDelete.mockRejectedValue(new Error('forbidden'))
    const r = await renderView({ allowDelete: true, canModerate: true })
    r.fireEvent.click(r.getByRole('button', { name: "Delete Bob's message" }))
    await r.waitFor(() => expect(apiDelete).toHaveBeenCalled())
    await new Promise(res => setTimeout(res, 20))
    expect(r.getByText('BOB')).toBeTruthy()
  })

  it('dm.message_deleted turns the bubble into the placeholder live', async () => {
    const r = await renderView({})
    // Another conversation's frame is ignored.
    emit('dm.message_deleted', { conversation_id: 'other', message_id: 'm-bob' })
    expect(r.getByText('BOB')).toBeTruthy()
    emit('dm.message_deleted', { conversation_id: 'sc-1', system_scope: 'space', space_id: 's1', message_id: 'm-bob' })
    await r.waitFor(() => expect(r.queryByText('BOB')).toBeNull())
    const el = bubble(r.container, 'm-bob')
    expect(el.className).toContain('sh-message--deleted')
    expect(el.textContent).toContain('(message deleted)')
    // Nothing of what was said stays — its reactions go too.
    expect(el.querySelector('.sh-reaction-strip')).toBeNull()
  })
})

describe('canDeleteMessage / asDeleted', () => {
  it('own or moderator; never a deleted, failed or unsent one', async () => {
    const { canDeleteMessage, asDeleted } = await import('./ConversationView')
    const m = msgRow('m1', 'x', 'u-bob') as unknown as Parameters<typeof canDeleteMessage>[0]
    expect(canDeleteMessage(m, 'u-me', false)).toBe(false)
    expect(canDeleteMessage(m, 'u-me', true)).toBe(true)
    expect(canDeleteMessage(m, 'u-bob', false)).toBe(true)
    expect(canDeleteMessage(m, null, true)).toBe(false)
    expect(canDeleteMessage({ ...m, deleted: true }, 'u-bob', true)).toBe(false)
    expect(canDeleteMessage({ ...m, send_failed: true }, 'u-bob', true)).toBe(false)
    expect(canDeleteMessage({ ...m, id: 'tmp-1' }, 'u-bob', true)).toBe(false)
    expect(asDeleted(m)).toMatchObject({ deleted: true, content: '', media_url: null, reactions: [] })
  })
})

describe('ConversationView — read-only (an archived space chat)', () => {
  it('no composer, reply, edit or reactions; the note and delete stay', async () => {
    const r = await renderView({
      allowDelete: true, readOnly: true, readOnlyNote: 'This space is archived.',
    })
    expect(r.container.querySelector('form.sh-composer')).toBeNull()
    expect(r.getByRole('note').textContent).toBe('This space is archived.')
    expect(r.queryByRole('button', { name: "Add reaction to Bob's message" })).toBeNull()
    expect(r.queryByRole('button', { name: 'Reply to Bob' })).toBeNull()
    expect(r.queryByRole('button', { name: 'Edit your message' })).toBeNull()
    // Existing reactions still show, but can't be toggled.
    const chip = bubble(r.container, 'm-bob').querySelector<HTMLButtonElement>('.sh-reaction-chip')!
    expect(chip.disabled).toBe(true)
    expect(r.getByRole('button', { name: 'Delete your message' })).toBeTruthy()
  })

  it('a writable thread keeps them all', async () => {
    const r = await renderView({ allowDelete: true })
    expect(r.container.querySelector('form.sh-composer')).not.toBeNull()
    expect(r.queryByRole('note')).toBeNull()
    expect(r.getByRole('button', { name: "Add reaction to Bob's message" })).toBeTruthy()
    expect(r.getByRole('button', { name: 'Reply to Bob' })).toBeTruthy()
    expect(r.getByRole('button', { name: 'Edit your message' })).toBeTruthy()
  })
})
