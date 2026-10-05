import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api, ApiError, _resetApiLoggedOut, setUnauthorizedHandler } from './api'

describe('api — surface', () => {
  it('exports an ApiClient instance', () => {
    expect(api).toBeTruthy()
    expect(typeof api.get).toBe('function')
    expect(typeof api.post).toBe('function')
    expect(typeof api.patch).toBe('function')
    expect(typeof api.delete).toBe('function')
  })
})

describe('ApiError — friendly-detail unwrap', () => {
  /** Replace ``global.fetch`` with a stub that returns the supplied
   *  ``status`` and parsed JSON body. Calling code only awaits
   *  ``res.json()`` once per request, so a single fixed body is enough. */
  function stubFetch(status: number, body: unknown) {
    const res = {
      ok: status >= 200 && status < 300,
      status,
      json: vi.fn().mockResolvedValue(body),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
  }

  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('parses {error: {code, detail}} into ApiError.code + .detail and uses detail as message', async () => {
    stubFetch(422, {
      error: {
        code: 'UNPROCESSABLE',
        detail: 'Home Assistant has no picture for this user.',
      },
    })
    try {
      await api.post('/api/me/picture/refresh-from-ha', {})
      expect.fail('should have thrown')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      const err = e as ApiError
      expect(err.status).toBe(422)
      expect(err.code).toBe('UNPROCESSABLE')
      expect(err.detail).toBe('Home Assistant has no picture for this user.')
      // ``Error.message`` is the detail — that's what
      // ``showToast(err.message)`` will display.
      expect(err.message).toBe('Home Assistant has no picture for this user.')
    }
  })

  it('turns a 403 ACCESS_ADMIN_ONLY into the translated "only admins" note', async () => {
    // Every surface toasts ``err.message`` — so an ADMIN_ONLY refusal of a
    // page / task / sticky / event / post write reads the same everywhere.
    stubFetch(403, {
      error: {
        code: 'ACCESS_ADMIN_ONLY',
        detail: 'Only space admins can change this here.',
        feature: 'pages',
      },
    })
    try {
      await api.post('/api/spaces/sp-1/pages', { title: 'x' })
      expect.fail('should have thrown')
    } catch (e) {
      const err = e as ApiError
      expect(err.code).toBe('ACCESS_ADMIN_ONLY')
      expect(err.extra.feature).toBe('pages')
      expect(err.message).toBe('Only admins can add or edit pages here.')
    }
  })

  it('turns a 409 HOST_TOO_OLD into the translated "host needs an update" toast', async () => {
    // v_43: a member household can't submit for review while its space's
    // host is too old to hold the item — every write surface toasts it.
    stubFetch(409, { error: { code: 'HOST_TOO_OLD', detail: 'raw detail' } })
    try {
      await api.post('/api/spaces/sp-1/stickies', { content: 'x' })
      expect.fail('should have thrown')
    } catch (e) {
      const err = e as ApiError
      expect(err.code).toBe('HOST_TOO_OLD')
      expect(err.message).toBe(
        "This space's host household needs an update before your changes can be reviewed.",
      )
    }
  })

  it('falls back to "API <status>: <path>" when the body is not the canonical shape', async () => {
    stubFetch(502, '<html>Bad Gateway</html>')
    try {
      await api.get('/api/whatever')
      expect.fail('should have thrown')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      const err = e as ApiError
      expect(err.status).toBe(502)
      expect(err.code).toBeNull()
      expect(err.detail).toBeNull()
      expect(err.message).toBe('API 502: /api/whatever')
    }
  })

  it('falls back to "API <status>: <path>" when the body is empty / unparseable', async () => {
    const res = {
      ok: false,
      status: 500,
      json: vi.fn().mockRejectedValue(new Error('not json')),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
    try {
      await api.delete('/api/foo')
      expect.fail('should have thrown')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      const err = e as ApiError
      expect(err.message).toBe('API 500: /api/foo')
    }
  })

  it('ignores garbage in the error body (non-string code / detail) and still sets status', async () => {
    stubFetch(404, { error: { code: 42, detail: ['nope'] } })
    try {
      await api.get('/api/foo')
      expect.fail('should have thrown')
    } catch (e) {
      const err = e as ApiError
      expect(err.code).toBeNull()
      expect(err.detail).toBeNull()
      expect(err.status).toBe(404)
      // No friendly detail to use — fall back to the historic shape.
      expect(err.message).toBe('API 404: /api/foo')
    }
  })

  it('exposes the extra structured fields of the error body (e.g. count, current_version)', async () => {
    stubFetch(409, {
      error: { code: 'DAYS_ORPHAN_ENTRIES', detail: '6 entries would be removed.', count: 6 },
    })
    try {
      await api.patch('/api/timetables/t1', { version: 1 })
      expect.fail('should have thrown')
    } catch (e) {
      const err = e as ApiError
      expect(err.code).toBe('DAYS_ORPHAN_ENTRIES')
      expect(err.extra).toEqual({ count: 6 })
    }
  })

  it('has an empty extra when the body carries nothing beyond code / detail', async () => {
    stubFetch(502, '<html>Bad Gateway</html>')
    try {
      await api.get('/api/whatever')
      expect.fail('should have thrown')
    } catch (e) {
      expect((e as ApiError).extra).toEqual({})
    }
  })
})

describe('ApiClient — empty / 204 responses', () => {
  // Regression for "The string did not match the expected pattern."
  // Safari (mobile + desktop WebKit) raises that error message from
  // ``JSON.parse('')``, which ``Response.json()`` calls under the
  // hood for an empty body. POST/PUT/PATCH need to recognize 204
  // and empty-body responses BEFORE invoking ``json()`` — both the
  // ``/api/remote_invites/{token}/accept`` flow Pascal's friend hit
  // and any other 204-returning endpoint.
  function stubEmptyResponse(opts: {
    status: number
    contentLength?: string | null
  }) {
    const json = vi.fn().mockImplementation(() => {
      throw new SyntaxError('The string did not match the expected pattern.')
    })
    const res = {
      ok: opts.status >= 200 && opts.status < 300,
      status: opts.status,
      headers: {
        get: (k: string) =>
          k.toLowerCase() === 'content-length'
            ? (opts.contentLength ?? null)
            : null,
      },
      json,
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
    return { json }
  }

  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('POST: returns null on a 204 without invoking res.json()', async () => {
    const { json } = stubEmptyResponse({ status: 204 })
    const result = await api.post('/api/remote_invites/abc/accept', {})
    expect(result).toBeNull()
    expect(json).not.toHaveBeenCalled()
  })

  it('POST: returns null on a 200 with Content-Length: 0', async () => {
    const { json } = stubEmptyResponse({ status: 200, contentLength: '0' })
    const result = await api.post('/api/some/empty-ok', {})
    expect(result).toBeNull()
    expect(json).not.toHaveBeenCalled()
  })

  it('PATCH/PUT: same 204 fast-path', async () => {
    stubEmptyResponse({ status: 204 })
    expect(await api.patch('/api/x', {})).toBeNull()
    stubEmptyResponse({ status: 204 })
    expect(await api.put('/api/x', {})).toBeNull()
  })
})

describe('ApiClient.postRaw — raw bodies with an explicit content type', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('POSTs the raw body base-relative (keeps the ingress prefix) with the given Content-Type', async () => {
    const res = {
      ok: true,
      status: 201,
      headers: { get: () => null },
      json: vi.fn().mockResolvedValue({ events: [] }),
    } as unknown as Response
    const fetchMock = vi.fn().mockResolvedValue(res)
    vi.stubGlobal('fetch', fetchMock)
    const out = await api.postRaw(
      '/api/calendars/c1/import_ics', 'BEGIN:VCALENDAR', 'text/calendar',
    )
    expect(out).toEqual({ events: [] })
    // Relative so it resolves against <base href> — under HA ingress
    // that is the /api/hassio_ingress/<token>/ prefix.
    expect(fetchMock.mock.calls[0][0]).toBe('api/calendars/c1/import_ics')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('POST')
    expect(init.body).toBe('BEGIN:VCALENDAR')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('text/calendar')
  })

  it('throws ApiError with the backend detail on a 422', async () => {
    const res = {
      ok: false,
      status: 422,
      json: vi.fn().mockResolvedValue({
        error: { code: 'ICS_PARSE_ERROR', detail: 'VEVENT missing SUMMARY' },
      }),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
    await expect(
      api.postRaw('/api/calendars/c1/import_ics', 'x', 'text/calendar'),
    ).rejects.toMatchObject({
      status: 422, code: 'ICS_PARSE_ERROR', message: 'VEVENT missing SUMMARY',
    })
  })
})

describe('ApiClient.delete — response body', () => {
  it('resolves to the JSON body when the server sends one', async () => {
    const res = {
      ok: true, status: 200,
      headers: { get: (k: string) => (k === 'content-type' ? 'application/json; charset=utf-8' : null) },
      json: vi.fn().mockResolvedValue({ timetable: { id: 't1' } }),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
    await expect(api.delete('/api/timetables/t1/entries/e1?version=2'))
      .resolves.toEqual({ timetable: { id: 't1' } })
    vi.unstubAllGlobals()
  })

  it('lets a malformed JSON body reject instead of hiding it as null', async () => {
    const res = {
      ok: true, status: 200,
      headers: { get: (k: string) => (k === 'content-type' ? 'application/json' : null) },
      json: vi.fn().mockRejectedValue(new SyntaxError('bad json')),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
    await expect(api.delete('/api/foo')).rejects.toThrow('bad json')
    vi.unstubAllGlobals()
  })

  it('resolves to null for a non-JSON body', async () => {
    const res = {
      ok: true, status: 200, headers: { get: () => 'text/plain' }, json: vi.fn(),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
    await expect(api.delete('/api/foo')).resolves.toBeNull()
    vi.unstubAllGlobals()
  })

  it('resolves to null for a 204', async () => {
    const res = {
      ok: true, status: 204, headers: { get: () => null }, json: vi.fn(),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
    await expect(api.delete('/api/foo')).resolves.toBeNull()
    vi.unstubAllGlobals()
  })
})

describe('ApiClient — request options (keepalive)', () => {
  function stub() {
    const res = { ok: true, status: 204, headers: new Headers(), json: vi.fn() } as unknown as Response
    const f = vi.fn().mockResolvedValue(res)
    vi.stubGlobal('fetch', f)
    return f
  }
  beforeEach(() => { vi.unstubAllGlobals() })

  it('DELETE forwards keepalive so a pagehide flush survives the unload', async () => {
    const f = stub()
    await api.delete('/api/shopping/x', { keepalive: true })
    expect(f.mock.calls[0][1]).toMatchObject({ method: 'DELETE', keepalive: true })
  })

  it('every verb accepts the options; omitted means a normal request', async () => {
    const f = stub()
    await api.post('/api/a', {}, { keepalive: true })
    await api.patch('/api/a', {}, { keepalive: true })
    await api.put('/api/a', {}, { keepalive: true })
    await api.delete('/api/a')
    expect(f.mock.calls.slice(0, 3).every(c => c[1].keepalive === true)).toBe(true)
    expect(f.mock.calls[3][1].keepalive).toBeUndefined()
  })
})

describe('ApiClient — 401 responses', () => {
  function stub401() {
    const res = {
      ok: false,
      status: 401,
      json: vi.fn().mockResolvedValue({
        error: { code: 'UNAUTHENTICATED', detail: 'Invalid credentials.' },
      }),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
  }

  beforeEach(async () => {
    vi.unstubAllGlobals()
    const { token } = await import('@/store/token')
    token.value = null
    _resetApiLoggedOut()
  })

  it('throws an ApiError carrying status 401 + the code (not a bare "Unauthorized")', async () => {
    stub401()
    const err = await api.post('/api/auth/token', {}).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(401)
    expect(err.code).toBe('UNAUTHENTICATED')
    expect(err.message).not.toBe('Unauthorized')
  })

  it('without a token (sign-in, ingress probe) it never logs out', async () => {
    stub401()
    const onUnauth = vi.fn()
    setUnauthorizedHandler(onUnauth)
    await api.get('/api/me').catch(() => {})
    expect(onUnauth).not.toHaveBeenCalled()
  })

  it('with a stashed token it logs out once (session expired)', async () => {
    const { token } = await import('@/store/token')
    token.value = 'tok'
    stub401()
    const onUnauth = vi.fn()
    setUnauthorizedHandler(onUnauth)
    await api.get('/api/me').catch(() => {})
    await api.get('/api/feed').catch(() => {})
    expect(onUnauth).toHaveBeenCalledTimes(1)
    token.value = null
  })
})
