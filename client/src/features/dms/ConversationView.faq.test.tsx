/**
 * ConversationView — "Add to FAQ" in an Ask-the-seller thread (v_75): the
 * seller turns a buyer's question into an entry of the listing's
 * questions and answers, pre-filled and reworded before it is published.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.setConfig({ testTimeout: 20_000 })
const RENDER_WAIT = 15_000

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: {}, path: '/' }),
  useLocation: () => ({ url: '/', route: vi.fn() }),
}))
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
    upload: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => {}, send: vi.fn() } }))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 't' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const msgRow = (id: string, content: string, sender: string, at: string) => ({
  id, sender_user_id: sender, content, type: 'text',
  media_url: null, file_name: null, mime_type: null,
  file_size_bytes: null, reply_to_id: null, reactions: [], deleted: false,
  created_at: at, edited_at: null,
})

const QUESTION = 'Hi! Does it fold flat?\nIt has to fit in my car.'

function listing(role: 'buyer' | 'seller', status = 'active') {
  return {
    post_id: 'post-1', space_id: 'sp-1', title: 'Bike', mode: 'fixed',
    status, price: 4000, start_price: null, winning_price: null,
    currency: 'EUR', image_urls: [], seller_user_id: role === 'seller' ? 'u-me' : 'u-ben',
    role, other_user_id: 'u-ben', writable: true, closes_at: null,
  }
}

function wire(role: 'buyer' | 'seller', status = 'active') {
  apiGet.mockImplementation(async (url: string) => {
    if (url.startsWith('/api/conversations/bzt-1/messages')) {
      // Newest first, as the API ships them.
      return [
        msgRow('m-3', 'Yes, it folds flat.', 'u-me', '2026-10-30T10:02:00+00:00'),
        msgRow('m-1', QUESTION, 'u-ben', '2026-10-30T10:00:00+00:00'),
      ]
    }
    if (url === '/api/conversations/bzt-1/members') {
      return [
        { user_id: 'u-me', username: 'me', display_name: 'Me', is_self: true },
        { user_id: 'u-ben', username: 'ben', display_name: 'Ben', is_self: false },
      ]
    }
    if (url === '/api/conversations/bzt-1') {
      return { id: 'bzt-1', type: 'dm', bazaar_listing: listing(role, status) }
    }
    return {}
  })
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPost.mockResolvedValue({ entry_id: 'e1', question: 'q', answer: 'a', position: 0 })
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
})

async function renderThread() {
  const tl = await import('@testing-library/preact')
  const { ConversationView } = await import('./ConversationView')
  const r = tl.render(<ConversationView conversationId="bzt-1" embedded showHeader={false} />)
  await tl.waitFor(
    () => expect(r.getByText('Yes, it folds flat.')).toBeTruthy(), { timeout: RENDER_WAIT },
  )
  // The listing card lands with the thread row.
  await tl.waitFor(() => expect(r.container.querySelector('.sh-listing-thread-card')).toBeTruthy())
  return { ...tl, ...r }
}

describe('ConversationView — Add to FAQ', () => {
  it('the seller turns the buyer\'s question into a pre-filled FAQ entry', async () => {
    wire('seller')
    const r = await renderThread()
    const add = await r.findByRole('button', { name: "Add Ben's question to your listing's FAQ" })
    // Only under the buyer's message — never under the seller's own.
    expect(r.getAllByRole('button', { name: /listing's FAQ/ })).toHaveLength(1)
    r.fireEvent.click(add)
    const question = await r.findByLabelText('Question') as HTMLTextAreaElement
    const answer = r.getByLabelText('Answer') as HTMLTextAreaElement
    // One line, the buyer's words; the seller's next reply as the answer.
    expect(question.value).toBe('Hi! Does it fold flat? It has to fit in my car.')
    expect(answer.value).toBe('Yes, it folds flat.')
    // The seller rewords before publishing; the entry never names Ben.
    r.fireEvent.input(question, { target: { value: 'Does it fold flat?' } })
    r.fireEvent.click(r.getByRole('button', { name: 'Publish' }))
    await r.waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/bazaar/post-1/faq',
      { question: 'Does it fold flat?', answer: 'Yes, it folds flat.' },
    ))
  })

  it('a buyer gets no Add to FAQ', async () => {
    wire('buyer')
    const r = await renderThread()
    await new Promise(res => setTimeout(res, 20))
    expect(r.queryByRole('button', { name: /FAQ/ })).toBeNull()
  })

  it('no Add to FAQ once the listing ended', async () => {
    wire('seller', 'sold')
    const r = await renderThread()
    await new Promise(res => setTimeout(res, 20))
    expect(r.queryByRole('button', { name: /FAQ/ })).toBeNull()
  })
})
