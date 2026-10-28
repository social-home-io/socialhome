/**
 * The push service worker (client/public/sw.js).
 *
 * A plain classic script served next to index.html, not part of the
 * bundle — tested here because this is where the JS test runner lives.
 * Each test runs it against a fake ServiceWorkerGlobalScope whose
 * registration scope is the HA ingress prefix, so every URL the worker
 * shows or opens must stay inside that prefix (never the origin root).
 */
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'fs'
import { resolve } from 'path'

const swSource = readFileSync(resolve(__dirname, '../public/sw.js'), 'utf-8')

const INGRESS_SCOPE = 'https://ha.local:8123/api/hassio_ingress/tok123/'

interface FakeClient {
  url: string
  focus: ReturnType<typeof vi.fn>
  navigate: ReturnType<typeof vi.fn>
}

interface FakeNotification {
  tag: string
  close: ReturnType<typeof vi.fn>
}

function shownNotification(tag: string): FakeNotification {
  return { tag, close: vi.fn() }
}

function bootWorker(scope: string, windows: FakeClient[] = [], shown: FakeNotification[] = []) {
  const listeners: Record<string, (event: unknown) => void> = {}
  const showNotification = vi.fn().mockResolvedValue(undefined)
  const getNotifications = vi.fn().mockResolvedValue(shown)
  const openWindow = vi.fn().mockResolvedValue(null)
  const matchAll = vi.fn().mockResolvedValue(windows)
  const self = {
    registration: { scope, showNotification, getNotifications },
    clients: { matchAll, openWindow },
    addEventListener: (type: string, fn: (event: unknown) => void) => {
      listeners[type] = fn
    },
  }
  new Function('self', swSource)(self)
  return { listeners, showNotification, getNotifications, openWindow, matchAll }
}

async function deliver(sw: ReturnType<typeof bootWorker>, data: unknown) {
  let pending: Promise<unknown> = Promise.resolve()
  let waited = false
  sw.listeners.push({
    data: { json: () => data },
    waitUntil: (p: Promise<unknown>) => {
      waited = true
      pending = p
    },
  })
  await pending
  return waited
}

async function push(sw: ReturnType<typeof bootWorker>, data: unknown) {
  let pending: Promise<unknown> = Promise.resolve()
  sw.listeners.push({
    data: { json: () => data },
    waitUntil: (p: Promise<unknown>) => {
      pending = p
    },
  })
  await pending
  const [title, options] = sw.showNotification.mock.calls[0]
  return { title, options }
}

async function click(sw: ReturnType<typeof bootWorker>, url: string | undefined) {
  let pending: Promise<unknown> = Promise.resolve()
  const close = vi.fn()
  sw.listeners.notificationclick({
    notification: { close, data: url === undefined ? {} : { url } },
    waitUntil: (p: Promise<unknown>) => {
      pending = p
    },
  })
  await pending
  expect(close).toHaveBeenCalled()
}

function windowAt(url: string, navigate?: ReturnType<typeof vi.fn>): FakeClient {
  const client: FakeClient = {
    url,
    focus: vi.fn(),
    navigate: navigate ?? vi.fn(),
  }
  client.focus.mockResolvedValue(client)
  if (!navigate) client.navigate.mockImplementation(async (to: string) => ({ ...client, url: to }))
  return client
}

describe('sw.js push under the ingress prefix', () => {
  it('deep-links to click_url (the field the server sends) inside the ingress prefix', async () => {
    const sw = bootWorker(INGRESS_SCOPE)
    const { title, options } = await push(sw, {
      title: 'Anna messaged you',
      click_url: '/dms/c-1',
      tag: 'dm_message:/dms/c-1',
    })
    expect(title).toBe('Anna messaged you')
    expect(options.data.url).toBe(`${INGRESS_SCOPE}dms/c-1`)
  })

  it('still accepts a legacy url field', async () => {
    const sw = bootWorker(INGRESS_SCOPE)
    const { options } = await push(sw, { title: 'x', url: '/spaces/s1' })
    expect(options.data.url).toBe(`${INGRESS_SCOPE}spaces/s1`)
  })

  it('keeps query and hash (missed call, settings anchor)', async () => {
    const sw = bootWorker(INGRESS_SCOPE)
    const { options } = await push(sw, { title: 'x', click_url: '/dms/c-1?missed=k1' })
    expect(options.data.url).toBe(`${INGRESS_SCOPE}dms/c-1?missed=k1`)
    const sw2 = bootWorker(INGRESS_SCOPE)
    const { options: o2 } = await push(sw2, { title: 'x', click_url: '/settings#privacy' })
    expect(o2.data.url).toBe(`${INGRESS_SCOPE}settings#privacy`)
  })

  it('never leaves the scope: an absolute or protocol-relative URL opens the app root', async () => {
    for (const bad of ['https://evil.example/x', '//evil.example/x', '/../../../lovelace']) {
      const sw = bootWorker(INGRESS_SCOPE)
      const { options } = await push(sw, { title: 'x', click_url: bad })
      expect(options.data.url.startsWith(INGRESS_SCOPE)).toBe(true)
    }
  })

  it('opens the app root when there is no link', async () => {
    const sw = bootWorker(INGRESS_SCOPE)
    const { options } = await push(sw, { title: 'x' })
    expect(options.data.url).toBe(INGRESS_SCOPE)
  })

  it('collapses repeats with the tag and does not re-alert', async () => {
    const sw = bootWorker(INGRESS_SCOPE)
    const { options } = await push(sw, { title: 'x', tag: 'dm_message:/dms/c-1' })
    expect(options.tag).toBe('dm_message:/dms/c-1')
    expect(options.renotify).toBe(false)
  })

  it('sets no tag when the server sends none', async () => {
    const sw = bootWorker(INGRESS_SCOPE)
    const { options } = await push(sw, { title: 'x' })
    expect('tag' in options).toBe(false)
  })

  it('falls back to a default title on a malformed payload', async () => {
    const sw = bootWorker('https://sh.example/')
    let pending: Promise<unknown> = Promise.resolve()
    sw.listeners.push({
      data: {
        json: () => {
          throw new SyntaxError('bad')
        },
      },
      waitUntil: (p: Promise<unknown>) => {
        pending = p
      },
    })
    await pending
    expect(sw.showNotification.mock.calls[0][0]).toBe('Social Home')
  })
})

describe('sw.js withdraw (a handled push comes off the device)', () => {
  it('closes the notifications with the withdrawn tags and shows nothing', async () => {
    const ring = shownNotification('call_missed:/dms/c-1')
    const dm = shownNotification('dm_message:/dms/c-1')
    const other = shownNotification('post_reacted:/post/p1')
    const sw = bootWorker(INGRESS_SCOPE, [], [ring, dm, other])
    const waited = await deliver(sw, {
      withdraw: ['call_missed:/dms/c-1', 'dm_message:/dms/c-1'],
      tag: 'call_missed:/dms/c-1',
    })
    expect(waited).toBe(true)
    expect(ring.close).toHaveBeenCalled()
    expect(dm.close).toHaveBeenCalled()
    expect(other.close).not.toHaveBeenCalled()
    expect(sw.showNotification).not.toHaveBeenCalled()
  })

  it('shows nothing when none of the withdrawn notifications is on screen', async () => {
    const sw = bootWorker(INGRESS_SCOPE, [], [])
    await deliver(sw, { withdraw: ['task_assigned:/tasks/t1'], tag: 'task_assigned:/tasks/t1' })
    expect(sw.showNotification).not.toHaveBeenCalled()
  })

  it('treats a push without a withdraw list as a normal notification', async () => {
    const sw = bootWorker(INGRESS_SCOPE, [], [shownNotification('x')])
    await deliver(sw, { title: 'Anna messaged you', tag: 'x', withdraw: 'x' })
    expect(sw.getNotifications).not.toHaveBeenCalled()
    expect(sw.showNotification).toHaveBeenCalledTimes(1)
  })
})

describe('sw.js notification click', () => {
  it('focuses an open window and moves it to the link instead of opening a new one', async () => {
    const open = windowAt(`${INGRESS_SCOPE}feed`)
    const sw = bootWorker(INGRESS_SCOPE, [open])
    await click(sw, `${INGRESS_SCOPE}dms/c-1`)
    expect(open.focus).toHaveBeenCalled()
    expect(open.navigate).toHaveBeenCalledWith(`${INGRESS_SCOPE}dms/c-1`)
    expect(sw.openWindow).not.toHaveBeenCalled()
  })

  it('does not reload a window already on the link', async () => {
    const open = windowAt(`${INGRESS_SCOPE}dms/c-1`)
    const sw = bootWorker(INGRESS_SCOPE, [windowAt(`${INGRESS_SCOPE}feed`), open])
    await click(sw, `${INGRESS_SCOPE}dms/c-1`)
    expect(open.focus).toHaveBeenCalled()
    expect(open.navigate).not.toHaveBeenCalled()
    expect(sw.openWindow).not.toHaveBeenCalled()
  })

  it('ignores windows outside the scope (HA Core frontend) and opens the link', async () => {
    const haCore = windowAt('https://ha.local:8123/lovelace/0')
    const sw = bootWorker(INGRESS_SCOPE, [haCore])
    await click(sw, `${INGRESS_SCOPE}spaces/s1`)
    expect(haCore.focus).not.toHaveBeenCalled()
    expect(sw.openWindow).toHaveBeenCalledWith(`${INGRESS_SCOPE}spaces/s1`)
  })

  it('opens a new window when the open one cannot be navigated', async () => {
    const open = windowAt(`${INGRESS_SCOPE}feed`, vi.fn().mockRejectedValue(new TypeError('uncontrolled')))
    const sw = bootWorker(INGRESS_SCOPE, [open])
    await click(sw, `${INGRESS_SCOPE}dms/c-1`)
    expect(sw.openWindow).toHaveBeenCalledWith(`${INGRESS_SCOPE}dms/c-1`)
  })

  it('opens the app root when the notification carries no link', async () => {
    const sw = bootWorker(INGRESS_SCOPE)
    await click(sw, undefined)
    expect(sw.openWindow).toHaveBeenCalledWith(INGRESS_SCOPE)
  })
})
