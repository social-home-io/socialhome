/* Cluster panel — operator-approved GFS cluster membership.
   A node joins the cluster only when its HELLO is signed with this GFS's
   own key (shared seed) or with a key the operator approved here, so the
   panel shows this node's key, takes a key on add, and marks each peer
   with where its key came from. */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'
import { ClusterPanel, keyFingerprint, peerErrorMessage } from './Cluster'

const OWN_KEY = '0123456789abcdef'.repeat(4)
const PINNED_KEY = 'fedcba9876543210'.repeat(4)

const clusterBody = {
  node_id: 'node-a',
  public_key: OWN_KEY,
  status: 'online',
  nodes: [
    {
      node_id: 'node-a', url: 'https://a.gfs.test', status: 'online',
      last_seen: '2026-06-11T18:00:00+00:00', connected_clients: 11,
      active_sync_sessions: 3, is_self: true,
      public_key: OWN_KEY, key_source: 'own',
    },
    {
      node_id: 'node-b', url: 'https://b.gfs.test', status: 'offline',
      last_seen: null, connected_clients: 0,
      active_sync_sessions: 0, is_self: false,
      public_key: OWN_KEY, key_source: 'own',
    },
    {
      node_id: 'node-c', url: 'https://c.gfs.test', status: 'online',
      last_seen: null, connected_clients: 2,
      active_sync_sessions: 0, is_self: false,
      public_key: PINNED_KEY, key_source: 'approved',
    },
    {
      node_id: 'node-d', url: 'https://d.gfs.test', status: 'unknown',
      last_seen: null, connected_clients: 0,
      active_sync_sessions: 0, is_self: false,
      public_key: '', key_source: 'none',
    },
  ],
}

interface Call { method: string; url: string; body: unknown }

/* Routes GET to the cluster body and lets a test decide the answer for
   writes. Records every call so tests can assert what was (not) sent. */
function stubApi(
  write: (c: Call) => { status: number; body: unknown } = () => ({ status: 201, body: {} }),
  getBody: unknown = clusterBody,
): Call[] {
  const calls: Call[] = []
  global.fetch = vi.fn(async (input, init) => {
    const url = typeof input === 'string' ? input : (input as Request).url
    const method = (init?.method || 'GET').toUpperCase()
    const body = init?.body ? JSON.parse(init.body as string) : undefined
    const call = { method, url, body }
    calls.push(call)
    if (method === 'GET') {
      return new Response(JSON.stringify(getBody), { status: 200 })
    }
    const r = write(call)
    return new Response(JSON.stringify(r.body), { status: r.status })
  }) as typeof fetch
  return calls
}

function rowFor(container: Element, nodeId: string): HTMLTableRowElement {
  return Array.from(container.querySelectorAll('tbody tr'))
    .find((r) => r.querySelector('.node-id')?.textContent === nodeId) as HTMLTableRowElement
}

function fill(container: Element, label: string, value: string) {
  const input = container.querySelector(`[aria-label="${label}"]`) as HTMLInputElement
  fireEvent.input(input, { target: { value } })
}

function fillValid(container: Element) {
  fill(container, 'Peer node id', 'node-z')
  fill(container, 'Peer URL', 'https://z.gfs.test')
  fill(container, 'Peer public key', PINNED_KEY)
}

function submit(container: Element) {
  fireEvent.submit(container.querySelector('form.add-peer')!)
}

beforeEach(() => {
  vi.restoreAllMocks()
})


describe('keyFingerprint', () => {
  it('shows the first 8 and last 8 hex characters in groups of four', () => {
    expect(keyFingerprint(OWN_KEY)).toBe('0123 4567 … 89ab cdef')
  })

  it('lower-cases and trims the key first', () => {
    expect(keyFingerprint(`  ${PINNED_KEY.toUpperCase()} `)).toBe('fedc ba98 … 7654 3210')
  })

  it('renders an em dash for a missing key', () => {
    expect(keyFingerprint('')).toBe('—')
    expect(keyFingerprint(null)).toBe('—')
  })
})


describe('peerErrorMessage', () => {
  it('maps every server error code to a plain message', () => {
    expect(peerErrorMessage('key_mismatch')).toBe(
      'This node is already approved with a different key. Remove it first to change its key.',
    )
    expect(peerErrorMessage('invalid_node_id')).toMatch(/node id/i)
    expect(peerErrorMessage('node_id_is_self')).toMatch(/this server's own node id/i)
    expect(peerErrorMessage('invalid_url')).toMatch(/http:\/\/ or https:\/\//)
    expect(peerErrorMessage('invalid_public_key')).toMatch(/64-character/)
  })

  it('falls back to a generic message for unknown codes', () => {
    expect(peerErrorMessage('something_new')).toMatch(/couldn't add/i)
  })
})


describe('ClusterPanel — this node', () => {
  it('shows this node\'s id, full public key and fingerprint with a copy action', async () => {
    stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    const self = container.querySelector('.self-node')!
    expect(self.textContent).toContain('node-a')
    expect(self.textContent).toContain(OWN_KEY)
    expect(self.textContent).toContain('0123 4567 … 89ab cdef')
    expect(self.textContent).toMatch(/give these two values to the operator of a peer GFS/i)
    expect(self.querySelector('button[aria-label="Copy public key"]')).toBeTruthy()
    expect(self.querySelector('button[aria-label="Copy node id"]')).toBeTruthy()
  })

  it('copies the public key to the clipboard and confirms', async () => {
    stubApi()
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    fireEvent.click(container.querySelector('button[aria-label="Copy public key"]')!)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(OWN_KEY))
    expect(await findByText('Public key copied')).toBeTruthy()
  })
})


describe('ClusterPanel — copy fallback', () => {
  it('selects the key for a manual copy when the clipboard is unavailable', async () => {
    stubApi()
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn(async () => { throw new Error('denied') }) },
      configurable: true,
    })
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    fireEvent.click(container.querySelector('button[aria-label="Copy public key"]')!)
    expect(await findByText(/couldn't copy automatically/i)).toBeTruthy()
    expect(window.getSelection()?.toString()).toBe(OWN_KEY)
  })
})


describe('ClusterPanel — peer list', () => {
  it('renders a row per node with counts, self marker and status pills', async () => {
    stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    expect(container.querySelectorAll('tbody tr')).toHaveLength(4)
    expect(rowFor(container, 'node-a').textContent).toContain('11')
    expect(rowFor(container, 'node-a').textContent).toContain('(this node)')
    expect(container.querySelector('.pill.active')).toBeTruthy()
    expect(container.querySelector('.pill.banned')).toBeTruthy()
  })

  it('shows a fingerprint per node', async () => {
    stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    expect(rowFor(container, 'node-c').textContent).toContain('fedc ba98 … 7654 3210')
    expect(rowFor(container, 'node-b').textContent).toContain('0123 4567 … 89ab cdef')
    expect(rowFor(container, 'node-d').querySelector('.fingerprint')?.textContent).toBe('—')
  })

  it('marks where each peer\'s key came from', async () => {
    stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    expect(rowFor(container, 'node-b').textContent).toContain("shares this server's key")
    expect(rowFor(container, 'node-c').textContent).toContain('approved key')
    expect(rowFor(container, 'node-d').textContent).toContain('not approved')
    expect(rowFor(container, 'node-d').querySelector('.key-source-none')).not.toBeNull()
    expect(rowFor(container, 'node-c').querySelector('.key-source-approved')).not.toBeNull()
  })

  it('tells the operator to re-add a peer that is not approved, not an approved one', async () => {
    stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    const hint = rowFor(container, 'node-d').querySelector('.key-hint')?.textContent ?? ''
    // Leads with the condition, points at the form below, and reassures a
    // shared-key sibling that it needs nothing.
    expect(hint).toMatch(/^If it has its own key, approve it again below with that key\./)
    expect(hint).toMatch(/rejoins by itself/)
    expect(rowFor(container, 'node-c').querySelector('.key-hint')).toBeNull()
    expect(rowFor(container, 'node-c').textContent).not.toContain('Make sure you added')
    expect(rowFor(container, 'node-b').querySelector('.key-hint')).toBeNull()
  })

  it('shows no Ping/Remove on the self row but does on a peer row', async () => {
    stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    expect(rowFor(container, 'node-a').querySelectorAll('button')).toHaveLength(0)
    expect(rowFor(container, 'node-b').textContent).toContain('Ping')
    expect(rowFor(container, 'node-b').textContent).toContain('Remove')
  })

  it('renders last_seen as UTC regardless of the viewer\'s local timezone', async () => {
    // Backend `cluster_nodes.last_seen` is the naive SQLite `datetime('now')`
    // shape (UTC by invariant). `new Date()` would parse it as local time.
    const prevTz = process.env.TZ
    process.env.TZ = 'America/New_York'
    try {
      stubApi(undefined, {
        ...clusterBody,
        nodes: [{ ...clusterBody.nodes[0], last_seen: '2026-06-11 18:00:00' }],
      })
      const { container } = render(<ClusterPanel />)
      await waitFor(() => expect(container.querySelectorAll('tbody tr')).toHaveLength(1))
      const expected = new Date('2026-06-11T18:00:00Z').toLocaleString()
      expect(container.querySelector('tbody tr')?.textContent).toContain(expected)
    } finally {
      process.env.TZ = prevTz
    }
  })
})


describe('ClusterPanel — remove', () => {
  it('asks for confirmation and warns the node cannot sync until re-added', async () => {
    const calls = stubApi(() => ({ status: 200, body: { ok: true } }))
    const { container, findByText, findByRole } = render(<ClusterPanel />)
    await findByText('node-b')
    fireEvent.click(Array.from(rowFor(container, 'node-c').querySelectorAll('button'))
      .find((b) => b.textContent === 'Remove')!)
    const dialog = await findByRole('alertdialog')
    expect(dialog.textContent).toContain('node-c')
    expect(dialog.textContent).toMatch(/can't sync with this server until you add it again/i)
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
    fireEvent.click(Array.from(dialog.querySelectorAll('button'))
      .find((b) => b.textContent === 'Remove node')!)
    await waitFor(() => expect(calls.some(
      (c) => c.method === 'DELETE' && c.url.endsWith('cluster/peers/node-c'),
    )).toBe(true))
  })

  it('says a shared-key node rejoins by itself instead of needing re-adding', async () => {
    stubApi()
    const { container, findByText, findByRole } = render(<ClusterPanel />)
    await findByText('node-b')
    fireEvent.click(Array.from(rowFor(container, 'node-b').querySelectorAll('button'))
      .find((b) => b.textContent === 'Remove')!)
    const dialog = await findByRole('alertdialog')
    expect(dialog.textContent).toMatch(/join again by itself/i)
    expect(dialog.textContent).not.toMatch(/until you add it again/i)
  })

  it('tells both cases apart for a node that is not approved', async () => {
    stubApi()
    const { container, findByText, findByRole } = render(<ClusterPanel />)
    await findByText('node-b')
    fireEvent.click(Array.from(rowFor(container, 'node-d').querySelectorAll('button'))
      .find((b) => b.textContent === 'Remove')!)
    const dialog = await findByRole('alertdialog')
    // Same message as the row hint: a shared-key sibling comes back by
    // itself, so the confirmation mustn't claim it needs re-adding.
    expect(dialog.textContent).toMatch(/shares this server's key, it joins again by itself/i)
    expect(dialog.textContent).toMatch(/has its own key, it can't sync .* until you approve it again/i)
  })

  it('moves focus to Cancel and closes on Escape', async () => {
    stubApi()
    const { container, findByText, findByRole, queryByRole } = render(<ClusterPanel />)
    await findByText('node-b')
    fireEvent.click(Array.from(rowFor(container, 'node-c').querySelectorAll('button'))
      .find((b) => b.textContent === 'Remove')!)
    const dialog = await findByRole('alertdialog')
    await waitFor(() => expect(document.activeElement?.textContent).toBe('Cancel'))
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(queryByRole('alertdialog')).toBeNull()
  })

  it('cancelling the confirmation sends nothing', async () => {
    const calls = stubApi()
    const { container, findByText, findByRole, queryByRole } = render(<ClusterPanel />)
    await findByText('node-b')
    fireEvent.click(Array.from(rowFor(container, 'node-b').querySelectorAll('button'))
      .find((b) => b.textContent === 'Remove')!)
    const dialog = await findByRole('alertdialog')
    fireEvent.click(Array.from(dialog.querySelectorAll('button'))
      .find((b) => b.textContent === 'Cancel')!)
    expect(queryByRole('alertdialog')).toBeNull()
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
  })

  it('surfaces an error when the confirmed Remove fails', async () => {
    stubApi(() => ({ status: 500, body: { detail: 'boom' } }))
    const { container, findByText, findByRole } = render(<ClusterPanel />)
    await findByText('node-b')
    fireEvent.click(Array.from(rowFor(container, 'node-b').querySelectorAll('button'))
      .find((b) => b.textContent === 'Remove')!)
    const dialog = await findByRole('alertdialog')
    fireEvent.click(Array.from(dialog.querySelectorAll('button'))
      .find((b) => b.textContent === 'Remove node')!)
    expect(await findByText('boom')).toBeTruthy()
  })
})


describe('ClusterPanel — add peer', () => {
  it('explains that shared-key nodes join automatically', async () => {
    stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    expect(container.querySelector('form.add-peer')?.textContent)
      .toMatch(/share this server's signing key join automatically/i)
  })

  it('posts node id, URL and public key, then clears the form', async () => {
    const calls = stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    fillValid(container)
    submit(container)
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    const post = calls.find((c) => c.method === 'POST')!
    expect(post.url).toBe('admin/api/cluster/peers')
    expect(post.body).toEqual({
      node_id: 'node-z', url: 'https://z.gfs.test', public_key: PINNED_KEY,
    })
    await waitFor(() => expect(
      (container.querySelector('[aria-label="Peer node id"]') as HTMLInputElement).value,
    ).toBe(''))
  })

  it('rejects a key that is not 64 hex characters without calling the server', async () => {
    const calls = stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    fillValid(container)
    fill(container, 'Peer public key', 'abc123')
    submit(container)
    expect(await findByText(/must be 64 hex characters/i)).toBeTruthy()
    expect((container.querySelector('[aria-label="Peer public key"]') as HTMLInputElement)
      .getAttribute('aria-invalid')).toBe('true')
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('rejects a non-http(s) URL without calling the server', async () => {
    const calls = stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    fillValid(container)
    fill(container, 'Peer URL', 'ftp://z.gfs.test')
    submit(container)
    expect(await findByText(/start with http:\/\/ or https:\/\//i)).toBeTruthy()
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('requires a node id', async () => {
    const calls = stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    fillValid(container)
    fill(container, 'Peer node id', '   ')
    submit(container)
    expect(await findByText(/enter the peer's node id/i)).toBeTruthy()
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('accepts an upper-case key and sends it trimmed', async () => {
    const calls = stubApi()
    const { container, findByText } = render(<ClusterPanel />)
    await findByText('node-b')
    fillValid(container)
    fill(container, 'Peer public key', ` ${PINNED_KEY.toUpperCase()} `)
    submit(container)
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    expect((calls.find((c) => c.method === 'POST')!.body as { public_key: string }).public_key)
      .toBe(PINNED_KEY.toUpperCase())
  })

  it('clears a stale error as soon as the operator edits the form', async () => {
    stubApi(() => ({ status: 422, body: { error: 'node_id_is_self' } }))
    const { container, findByRole, queryByRole } = render(<ClusterPanel />)
    await findByRole('table')
    fillValid(container)
    submit(container)
    await findByRole('alert')
    const nodeInput = container.querySelector('[aria-label="Peer node id"]') as HTMLInputElement
    expect(nodeInput.getAttribute('aria-invalid')).toBe('true')
    fill(container, 'Peer node id', 'node-y')
    expect(queryByRole('alert')).toBeNull()
    expect(nodeInput.getAttribute('aria-invalid')).toBe('false')
  })

  it('clears a field\'s validation message when that field is edited', async () => {
    stubApi()
    const { container, findByText, findByRole, queryByText } = render(<ClusterPanel />)
    await findByRole('table')
    fillValid(container)
    fill(container, 'Peer URL', 'ftp://z')
    submit(container)
    await findByText(/start with http:\/\/ or https:\/\//i)
    fill(container, 'Peer URL', 'https://z.gfs.test')
    expect(queryByText(/start with http:\/\/ or https:\/\//i)).toBeNull()
  })

  it.each([
    [409, 'key_mismatch', 'This node is already approved with a different key. Remove it first to change its key.'],
    [422, 'invalid_public_key', /64-character/],
    [422, 'node_id_is_self', /this server's own node id/i],
    [422, 'invalid_url', /http:\/\/ or https:\/\//],
    [422, 'invalid_node_id', /node id/i],
  ])('maps a %s %s from the server to a plain message', async (status, code, expected) => {
    stubApi(() => ({ status, body: { error: code } }))
    const { container, findByText, findByRole } = render(<ClusterPanel />)
    await findByText('node-b')
    fillValid(container)
    submit(container)
    const alert = await findByRole('alert')
    if (typeof expected === 'string') expect(alert.textContent).toBe(expected)
    else expect(alert.textContent).toMatch(expected)
    // The message sits under the field it is about and describes it.
    const input = alert.closest('.field')?.querySelector('input')
    expect(input?.getAttribute('aria-invalid')).toBe('true')
    expect(input?.getAttribute('aria-describedby')).toBe(alert.id)
    // The form keeps what the operator typed so they can fix it.
    expect((container.querySelector('[aria-label="Peer node id"]') as HTMLInputElement).value)
      .toBe('node-z')
  })
})
