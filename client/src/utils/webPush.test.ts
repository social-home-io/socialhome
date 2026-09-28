import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  }
})

import { api, ApiError } from '@/api'
import {
  disableWebPush, enableWebPush, pushSubscriptionId, webPushSupported,
} from './webPush'

const ENDPOINT = 'https://push.example.com/send/abc'

function fakeSubscription() {
  return {
    endpoint: ENDPOINT,
    unsubscribe: vi.fn().mockResolvedValue(true),
    toJSON: () => ({ endpoint: ENDPOINT, keys: { p256dh: 'dh', auth: 'au' } }),
  }
}

function installPushEnv(existing: ReturnType<typeof fakeSubscription> | null) {
  const created = fakeSubscription()
  const pushManager = {
    getSubscription: vi.fn().mockResolvedValue(existing),
    subscribe: vi.fn().mockResolvedValue(created),
  }
  const reg = { pushManager }
  const serviceWorker = {
    register: vi.fn().mockResolvedValue(reg),
    getRegistration: vi.fn().mockResolvedValue(reg),
    ready: Promise.resolve(reg),
  }
  Object.defineProperty(navigator, 'serviceWorker', { value: serviceWorker, configurable: true })
  ;(window as unknown as Record<string, unknown>).PushManager = function PushManager() {}
  vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('granted') })
  return { serviceWorker, pushManager, created }
}

describe('webPush', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
    vi.mocked(api.delete).mockReset()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    delete (navigator as unknown as Record<string, unknown>).serviceWorker
    delete (window as unknown as Record<string, unknown>).PushManager
  })

  it('derives a stable, endpoint-bound row id', async () => {
    const a = await pushSubscriptionId(ENDPOINT)
    expect(a).toMatch(/^sub-[0-9a-f]{32}$/)
    expect(await pushSubscriptionId(ENDPOINT)).toBe(a)
    expect(await pushSubscriptionId(ENDPOINT + 'x')).not.toBe(a)
  })

  it('reports unsupported browsers', () => {
    expect(webPushSupported()).toBe(false)
  })

  it('enable: registers the SW relatively, uses the real VAPID route, POSTs the subscription with its id', async () => {
    const env = installPushEnv(null)
    vi.mocked(api.get).mockResolvedValue({ public_key: 'AQID' })
    vi.mocked(api.post).mockResolvedValue({ id: 'x' })

    expect(await enableWebPush()).toBe(true)

    // Relative — resolves under the ingress prefix.
    expect(env.serviceWorker.register).toHaveBeenCalledWith('sw.js')
    expect(api.get).toHaveBeenCalledWith('/api/push/vapid_public_key')
    const opts = env.pushManager.subscribe.mock.calls[0][0]
    expect(opts.userVisibleOnly).toBe(true)
    expect(Array.from(opts.applicationServerKey as Uint8Array)).toEqual([1, 2, 3])
    expect(api.post).toHaveBeenCalledWith('/api/push/subscribe', {
      endpoint: ENDPOINT,
      keys: { p256dh: 'dh', auth: 'au' },
      id: await pushSubscriptionId(ENDPOINT),
    })
  })

  it('enable: returns false and registers nothing when permission is declined', async () => {
    const env = installPushEnv(null)
    vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('denied') })
    expect(await enableWebPush()).toBe(false)
    expect(env.serviceWorker.register).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
  })

  it('disable: DELETEs /api/push/subscribe/{id} for this endpoint, then unsubscribes the browser', async () => {
    const sub = fakeSubscription()
    installPushEnv(sub)
    vi.mocked(api.delete).mockResolvedValue(undefined)

    await disableWebPush()

    const id = await pushSubscriptionId(ENDPOINT)
    expect(api.delete).toHaveBeenCalledWith(`/api/push/subscribe/${id}`)
    expect(api.post).not.toHaveBeenCalled()
    expect(sub.unsubscribe).toHaveBeenCalled()
  })

  it('disable: a 404 (row already gone) still unsubscribes the browser', async () => {
    const sub = fakeSubscription()
    installPushEnv(sub)
    vi.mocked(api.delete).mockRejectedValue(new ApiError(404, '/api/push/subscribe/x'))
    await disableWebPush()
    expect(sub.unsubscribe).toHaveBeenCalled()
  })

  it('disable: a server failure propagates and keeps the browser subscription', async () => {
    const sub = fakeSubscription()
    installPushEnv(sub)
    vi.mocked(api.delete).mockRejectedValue(new ApiError(500, '/api/push/subscribe/x'))
    await expect(disableWebPush()).rejects.toBeInstanceOf(ApiError)
    expect(sub.unsubscribe).not.toHaveBeenCalled()
  })

  it('disable: no-op without a subscription', async () => {
    installPushEnv(null)
    await disableWebPush()
    expect(api.delete).not.toHaveBeenCalled()
  })
})
