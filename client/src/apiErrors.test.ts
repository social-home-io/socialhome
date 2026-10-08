import { describe, it, expect, afterEach } from 'vitest'
import { apiErrorMessage } from './apiErrors'
import { setLocale } from '@/i18n/i18n'

describe('apiErrorMessage — known codes', () => {
  afterEach(async () => { await setLocale('en') })

  it('translates a known code instead of showing the English detail', () => {
    expect(apiErrorMessage(422, '/api/conversations/dm', {
      code: 'DM_SELF', detail: 'cannot DM yourself',
    })).toBe("You can't start a conversation with yourself. Pick someone else.")
  })

  it('translates the GFS fallback refusal for a household not paired directly', () => {
    expect(apiErrorMessage(409, '/api/pairing/connections/p1', {
      code: 'GFS_RELAY_NOT_ALLOWED',
      detail: 'The GFS fallback is only for confirmed, directly paired households.',
    })).toBe('Only households you paired with directly can use the GFS fallback.')
  })

  it('fills params into the translated message', () => {
    expect(apiErrorMessage(422, '/x', {
      code: 'DM_TOO_LONG', detail: 'message content exceeds 1000 chars', params: { max: 1000 },
    })).toBe('That message is too long. Keep it to 1000 characters.')
    expect(apiErrorMessage(403, '/x', {
      code: 'AGE_RESTRICTED', detail: 'x', params: { min_age: 16 },
    })).toBe('This space is for people aged 16 and up.')
  })

  it('translates a refused space link and DM attachment instead of the English rule', () => {
    expect(apiErrorMessage(422, '/api/spaces/s1/links', {
      code: 'INVALID_LINK', detail: 'url must be an http(s) web address',
    })).toBe('Use a full web address that starts with https://.')
    expect(apiErrorMessage(422, '/api/conversations/c1/messages', {
      code: 'INVALID_MEDIA_URL', detail: 'media_url must be a file uploaded via /api/media/upload',
    })).toBe("That file can't be sent. Attach it again and resend.")
  })

  it('formats the bid floor as money in the listing currency', () => {
    const msg = apiErrorMessage(422, '/api/bazaar/p1/bids', {
      code: 'BID_TOO_LOW',
      detail: 'amount must be at least 1500',
      params: { floor_amount: 1500, currency: 'EUR' },
    })
    expect(msg).toMatch(/^Your bid is too low\. Bid at least /)
    expect(msg).toContain('15')
    expect(msg).not.toContain('1500')
    expect(msg).toMatch(/€|EUR/)
  })

  it('a zero-decimal currency is not divided by 100', () => {
    const msg = apiErrorMessage(422, '/x', {
      code: 'BID_TOO_LOW', detail: 'x', params: { floor_amount: 1500, currency: 'JPY' },
    })
    expect(msg.replace(/\D/g, '')).toBe('1500')
  })

  it('a bid floor with an unusable currency keeps the server detail', () => {
    expect(apiErrorMessage(422, '/x', {
      code: 'BID_TOO_LOW', detail: 'amount must be at least 1500', params: { floor_amount: 1500, currency: 'NOT A CODE' },
    })).toBe('amount must be at least 1500')
  })

  it('picks the group-member reason and names the person', () => {
    expect(apiErrorMessage(422, '/x', {
      code: 'GROUP_MEMBER_UNSUPPORTED', detail: 'x', params: { reason: 'too_old', name: 'Ana' },
    })).toBe("Ana's household needs a Social Home update before they can join group chats.")
    // An unknown reason keeps the server's sentence.
    expect(apiErrorMessage(422, '/x', {
      code: 'GROUP_MEMBER_UNSUPPORTED', detail: 'server words', params: { reason: 'new' },
    })).toBe('server words')
  })

  it('keeps the existing ACCESS_ADMIN_ONLY / HOST_* mappings', () => {
    expect(apiErrorMessage(403, '/x', {
      code: 'ACCESS_ADMIN_ONLY', detail: 'x', feature: 'pages',
    })).toBe('Only admins can add or edit pages here.')
  })

  it('renders German in German', async () => {
    await setLocale('de')
    expect(apiErrorMessage(422, '/x', { code: 'DM_SELF', detail: 'cannot DM yourself' }))
      .toBe('Du kannst keine Unterhaltung mit dir selbst beginnen. Wähl jemand anderen aus.')
    expect(apiErrorMessage(403, '/x', { code: 'SUBSCRIBER_READ_ONLY', detail: 'x', params: { action: 'post' } }))
      .toContain('Du folgst diesem Space')
    expect(apiErrorMessage(503, '/x', null))
      .toBe('Auf dem Server ist etwas schiefgelaufen. Versuch es gleich noch einmal.')
  })
})

describe('apiErrorMessage — fallbacks', () => {
  it('an unknown, specific code keeps the server detail', () => {
    expect(apiErrorMessage(409, '/x', { code: 'SOMETHING_NEW', detail: 'A specific reason.' }))
      .toBe('A specific reason.')
  })

  it('generic codes get the per-status line', () => {
    expect(apiErrorMessage(404, '/x', { code: 'NOT_FOUND', detail: 'Resource not found.' }))
      .toBe('Not found — it may have been deleted or moved.')
    expect(apiErrorMessage(429, '/x', { code: 'RATE_LIMITED', detail: '' }))
      .toBe('Too many tries — wait a moment and try again.')
    expect(apiErrorMessage(500, '/x', { code: 'INTERNAL_ERROR', detail: '' }))
      .toBe('Something went wrong on the server. Try again in a moment.')
  })

  it('the blanket 422 gets the translated line; a specific 422 keeps its detail', () => {
    expect(apiErrorMessage(422, '/x', { code: 'UNPROCESSABLE', detail: 'Request could not be processed.' }))
      .toBe("Couldn't do that. Check your input and try again.")
    expect(apiErrorMessage(422, '/x', { code: 'UNPROCESSABLE', detail: 'amount is required.' }))
      .toBe('amount is required.')
  })

  it('a 5xx outage the user can act on is translated', () => {
    expect(apiErrorMessage(502, '/x', { code: 'GFS_UNAVAILABLE', detail: 'upstream said no' }))
      .toBe("Couldn't reach the GFS. Try again in a moment.")
    expect(apiErrorMessage(507, '/x', { code: 'STORAGE_FULL', detail: 'disk' }))
      .toBe('Your Social Home is out of storage space. Free up space or ask your admin.')
  })

  it.each([
    [502, 'GFS_UNREACHABLE', "Couldn't reach the GFS. Try again in a moment."],
    [409, 'GFS_SIGNUP_CLOSED', "This GFS isn't taking sign-ups right now. Ask its operator for a pairing code."],
    [503, 'GFS_BUSY', 'The GFS is busy. Try again in a minute.'],
    [422, 'GFS_IDENTITY_MISMATCH', "This doesn't look like the Social Home GFS. Check the address."],
    [422, 'GFS_PAIRING_FAILED', "The GFS didn't accept this household."],
    [409, 'ALREADY_CONNECTED', "You're already connected to this GFS."],
  ])('a GFS connect refusal (%i %s) is translated, never the server detail', (status, code, text) => {
    expect(apiErrorMessage(status, '/api/gfs/connections', { code, detail: 'server words' })).toBe(text)
  })

  it('a 5xx with a specific code keeps its detail; a generic one gets the server line', () => {
    expect(apiErrorMessage(504, '/x', { code: 'ISSUER_TIMEOUT', detail: 'The issuer did not answer.' }))
      .toBe('The issuer did not answer.')
    for (const body of [{ code: 'INTERNAL_ERROR', detail: 'boom' }, { detail: 'boom' }, null]) {
      expect(apiErrorMessage(500, '/x', body)).toBe('Something went wrong on the server. Try again in a moment.')
    }
  })

  it('a body-less error with no per-status line keeps "API <status>: <path>"', () => {
    expect(apiErrorMessage(409, '/api/foo', null)).toBe('API 409: /api/foo')
  })
})
