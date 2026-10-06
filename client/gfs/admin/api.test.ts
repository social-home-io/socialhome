import { describe, expect, it, vi, beforeEach } from 'vitest'
import { api, ApiError, UnauthorizedError } from './api'

function respond(status: number, body: unknown) {
  global.fetch = vi.fn(async () =>
    new Response(JSON.stringify(body), { status })) as typeof fetch
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('api', () => {
  it('resolves a relative path so it works behind a path prefix', async () => {
    respond(200, { ok: true })
    await api('GET', '/admin/api/overview')
    expect((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toBe('admin/api/overview')
  })

  it('throws UnauthorizedError on 401', async () => {
    respond(401, {})
    await expect(api('GET', '/admin/api/overview')).rejects.toBeInstanceOf(UnauthorizedError)
  })

  it('exposes the {error} code and status of a failed call', async () => {
    respond(409, { error: 'key_mismatch' })
    const err = (await api('POST', '/admin/api/cluster/peers', {}).catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(409)
    expect(err.code).toBe('key_mismatch')
  })

  it('keeps {detail} as the message and has no code when none is sent', async () => {
    respond(500, { detail: 'boom' })
    const err = (await api('DELETE', '/admin/api/cluster/peers/x').catch((e: unknown) => e)) as ApiError
    expect(err.message).toBe('boom')
    expect(err.code).toBeNull()
  })
})
