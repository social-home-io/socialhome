import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiPost = vi.fn()
vi.mock('@/api', () => ({ api: { post: (...a: unknown[]) => apiPost(...a), get: vi.fn() } }))

type Reply = { status: number; body?: string; retryAfter?: string; network?: boolean; hold?: boolean }
let replies: Map<string, Reply[]>
let live: FakeXhr[]
const sent: Array<{ name: string; url: string }> = []

class FakeXhr {
  status = 0
  responseText = ''
  upload: { onprogress?: (e: unknown) => void; onload?: () => void } = {}
  onload?: () => void
  onerror?: () => void
  onabort?: () => void
  withCredentials = false
  url = ''
  name = ''
  private retryAfter: string | null = null
  private settled = false
  open(_m: string, url: string) { this.url = url }
  setRequestHeader() {}
  getResponseHeader(n: string) { return n === 'Retry-After' ? this.retryAfter : null }
  abort() { if (this.settled) return; this.settled = true; this.onabort?.() }
  send(fd: FormData) {
    const f = fd.get('file') as File
    this.name = f.name
    sent.push({ name: f.name, url: this.url })
    live.push(this)
    const r = (replies.get(f.name) ?? []).shift() ?? { status: 201, body: JSON.stringify({ id: 'i', item_type: 'photo', thumbnail_url: 't' }) }
    if (r.hold) return
    setTimeout(() => this.finish(r), 0)
  }
  finish(r: Reply) {
    if (this.settled) return
    this.settled = true
    if (r.network) { this.onerror?.(); return }
    this.status = r.status
    this.responseText = r.body ?? ''
    this.retryAfter = r.retryAfter ?? null
    this.upload.onload?.()
    this.onload?.()
  }
}

type Q = typeof import('./uploadQueue')
let q: Q
const jpg = (n: string) => new File(['x'], n, { type: 'image/jpeg' })
const album = { spaceId: null, albumId: 'al-1', name: 'Holiday', isDefault: false }
const tick = (ms = 5) => new Promise(r => setTimeout(r, ms))
async function settle(): Promise<void> {
  for (let i = 0; i < 200 && !q.counts.value.settled; i++) await tick()
}

beforeEach(async () => {
  replies = new Map()
  live = []
  sent.length = 0
  apiPost.mockReset()
  vi.stubGlobal('XMLHttpRequest', FakeXhr)
  vi.resetModules()
  q = await import('./uploadQueue')
})
afterEach(() => { q._resetForTests(); vi.unstubAllGlobals() })

describe('upload queue', () => {
  it('sends three at a time, not more', async () => {
    for (const n of ['a', 'b', 'c', 'd', 'e']) replies.set(`${n}.jpg`, [{ status: 201, hold: true }])
    q.addFiles(['a', 'b', 'c', 'd', 'e'].map(n => jpg(`${n}.jpg`)), album)
    await tick()
    expect(sent.map(s => s.name)).toEqual(['a.jpg', 'b.jpg', 'c.jpg'])
    expect(q.counts.value.waiting).toBe(2)
    live[0].finish({ status: 201, body: '{"id":"x","item_type":"photo"}' })
    await tick()
    expect(sent).toHaveLength(4)
  })

  it('a 429 holds every worker until Retry-After, then sends the same file', async () => {
    replies.set('a.jpg', [{ status: 429, retryAfter: '0.05' }])
    q.addFiles([jpg('a.jpg')], album)
    await tick()
    expect(q.slowUntil.value).toBeGreaterThan(Date.now())
    q.addFiles([jpg('b.jpg')], album)
    await tick()
    expect(sent.map(s => s.name)).toEqual(['a.jpg'])
    await settle()
    expect(sent.map(s => s.name).sort()).toEqual(['a.jpg', 'a.jpg', 'b.jpg'])
    expect(q.counts.value.done).toBe(2)
  })

  it('a lost connection is tried again, then fails with a reason', async () => {
    replies.set('a.jpg', [{ status: 0, network: true }])
    q.addFiles([jpg('a.jpg')], album)
    await settle()
    expect(q.counts.value.done).toBe(1)
    expect(sent).toHaveLength(2)
  })

  it('pause puts files back; resume carries on', async () => {
    replies.set('a.jpg', [{ status: 201, hold: true }])
    q.addFiles([jpg('a.jpg'), jpg('b.jpg')], album)
    await tick()
    q.pause()
    await tick()
    expect(q.entries.value.every(e => e.state === 'waiting' || e.state === 'done')).toBe(true)
    const before = sent.length
    await tick(20)
    expect(sent.length).toBe(before)
    q.resume()
    await settle()
    expect(q.counts.value.done).toBe(2)
  })

  it('remove stops one file; cancel stops the rest and keeps what is done', async () => {
    replies.set('b.jpg', [{ status: 201, hold: true }])
    replies.set('c.jpg', [{ status: 201, hold: true }])
    replies.set('d.jpg', [{ status: 201, hold: true }])
    replies.set('e.jpg', [{ status: 201, hold: true }])
    q.addFiles(['a', 'b', 'c', 'd', 'e'].map(n => jpg(`${n}.jpg`)), album)
    await tick(20)
    const b = q.entries.value.find(e => e.file.name === 'b.jpg')!
    q.remove(b.id)
    await tick()
    expect(q.entries.value.some(e => e.file.name === 'b.jpg')).toBe(false)
    q.cancelAll()
    await tick()
    expect(q.entries.value.map(e => e.file.name)).toEqual(['a.jpg'])
    expect(q.counts.value.settled).toBe(true)
    expect(q.removedCount.value).toBe(4)
  })

  it('a full disk stops the queue and says why', async () => {
    replies.set('a.jpg', [{ status: 507, body: JSON.stringify({ error: { code: 'STORAGE_FULL' } }) }])
    replies.set('b.jpg', [{ status: 201, hold: true }])
    q.addFiles([jpg('a.jpg'), jpg('b.jpg'), jpg('c.jpg'), jpg('d.jpg')], album)
    await tick(20)
    expect(q.paused.value).toBe(true)
    expect(q.pauseReason.value).toBe('storage_full')
    const a = q.entries.value.find(e => e.file.name === 'a.jpg')!
    expect(a.state).toBe('failed')
    expect(a.reason).toBe("The household's storage is full.")
    expect(q.counts.value.uploading).toBe(0)
  })

  it('retryAll after a full disk resumes', async () => {
    replies.set('a.jpg', [{ status: 507, body: '{}' }])
    q.addFiles([jpg('a.jpg')], album)
    await tick(20)
    q.retryAll()
    await settle()
    expect(q.paused.value).toBe(false)
    expect(q.counts.value.done).toBe(1)
  })

  it('no album chosen: the default album is asked for once, then used', async () => {
    let resolve!: (v: unknown) => void
    apiPost.mockImplementation(() => new Promise(r => { resolve = r }))
    q.addFiles([jpg('a.jpg'), jpg('b.jpg'), jpg('c.jpg')], { spaceId: null, albumId: null, name: 'Photos', isDefault: true })
    await tick()
    expect(apiPost).toHaveBeenCalledTimes(1)
    resolve({ id: 'al-def', name: 'Fotos' })
    await settle()
    expect(sent.map(s => s.url)).toEqual(Array(3).fill('api/gallery/albums/al-def/items'))
    expect(q.target.value).toMatchObject({ albumId: 'al-def', name: 'Fotos', isDefault: true })
  })

  it('the default album failing fails the file with a reason; Try again asks again', async () => {
    apiPost.mockRejectedValueOnce(new Error('API 500: /api/gallery/default-album'))
    apiPost.mockResolvedValueOnce({ id: 'al-def', name: 'Photos' })
    q.addFiles([jpg('a.jpg')], { spaceId: null, albumId: null, name: 'Photos', isDefault: true })
    await settle()
    const a = q.entries.value[0]
    expect(a.state).toBe('failed')
    expect(a.reason).toBe("Your home couldn't save it. Try again in a moment.")
    q.retry(a.id)
    await settle()
    expect(q.counts.value.done).toBe(1)
  })

  it('without an album nothing starts until one is set', async () => {
    q.addFiles([jpg('a.jpg')], null)
    await tick(10)
    expect(sent).toEqual([])
    q.setTarget(album)
    await settle()
    expect(sent[0].url).toBe('api/gallery/albums/al-1/items')
  })

  it('refuses a busy queue of another gallery', async () => {
    replies.set('a.jpg', [{ status: 201, hold: true }])
    q.addFiles([jpg('a.jpg')], album)
    expect(q.addFiles([jpg('b.jpg')], { spaceId: 'sp-1', albumId: 'x', name: 'X', isDefault: false })).toBe(false)
    expect(q.entries.value).toHaveLength(1)
  })

  it('keeps the screen on while busy and lets go when done (fail-soft)', async () => {
    const release = vi.fn(async () => undefined)
    const request = vi.fn(async () => ({ release }))
    Object.defineProperty(navigator, 'wakeLock', { value: { request }, configurable: true })
    replies.set('a.jpg', [{ status: 201, hold: true }])
    q.addFiles([jpg('a.jpg')], album)
    await tick()
    expect(request).toHaveBeenCalledWith('screen')
    live[0].finish({ status: 201, body: '{}' })
    await settle()
    await tick()
    expect(release).toHaveBeenCalled()
    request.mockRejectedValueOnce(new Error('NotAllowedError'))
    q.addFiles([jpg('b.jpg')], album)
    await settle()
    expect(q.counts.value.done).toBe(1)
    // @ts-expect-error test cleanup
    delete navigator.wakeLock
  })

  it('maps server answers to short, translated reasons', () => {
    expect(q.errorReason(new Error('Network error'))).toBe('Connection lost.')
    expect(q.problemReason('unsupported')).toMatch(/^Not a photo or video/)
  })
})
