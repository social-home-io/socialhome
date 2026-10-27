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

  it('translates a highlight frame refused for another audience', () => {
    expect(apiErrorMessage(409, '/api/highlights/frames', {
      code: 'HIGHLIGHT_AUDIENCE_MISMATCH',
      detail: 'Today\'s highlight is for different people.',
      params: { audience_kind: 'all_paired', audience_count: 0 },
    })).toBe("Today's highlight is for other people. New frames go to the same people as today's highlight.")
  })

  it('translates the two refusals of changing today\'s highlight audience', () => {
    expect(apiErrorMessage(409, '/api/highlights/h1/audience', {
      code: 'HIGHLIGHT_NOT_TODAY', detail: "Only today's highlight can change who sees it.",
    })).toBe("Only today's highlight can change who sees it. An earlier day keeps its people.")
    expect(apiErrorMessage(422, '/api/highlights/h1/audience', {
      code: 'HIGHLIGHT_AUDIENCE_EMPTY', detail: 'Pick at least one person.',
    })).toBe('Pick at least one household or person.')
    expect(apiErrorMessage(409, '/api/highlights/h1/audience', {
      code: 'HIGHLIGHT_AUDIENCE_PEER_TOO_OLD', detail: 'Some paired households need to update.',
    })).toBe(
      "Some paired households need to update Social Home before you can take today's highlight away"
      + ' from people there. Adding people works now.',
    )
  })

  it('translates the two refusals of saving a moment', () => {
    expect(apiErrorMessage(422, '/api/moments/m1/save', {
      code: 'OWN_MOMENT', detail: 'Your own moments already stay in your archive.',
    })).toBe('Your own moments already stay under Yours.')
    expect(apiErrorMessage(422, '/api/moments/m1/save', {
      code: 'SAVE_UNAVAILABLE', detail: 'This moment can\'t be saved.',
    })).toContain("This moment can't be saved")
  })

  it('translates the GFS fallback refusal for a household not paired directly', () => {
    expect(apiErrorMessage(409, '/api/pairing/connections/p1', {
      code: 'GFS_RELAY_NOT_ALLOWED',
      detail: 'The GFS fallback is only for confirmed, directly paired households.',
    })).toBe('Only households you paired with directly can use the GFS fallback.')
  })

  it('translates a cancel or approval that raced a pairing going live', () => {
    expect(apiErrorMessage(409, '/api/pairing/pending/p1', {
      code: 'PAIRING_CONFIRMED',
      detail: 'This pairing is already confirmed — remove the connection instead.',
    })).toBe('This household is already paired with you.')
  })

  it('translates an expired trust-relay pairing request', () => {
    expect(apiErrorMessage(410, '/api/pairing/auto-pair-requests/r1/approve', {
      code: 'REQUEST_EXPIRED',
      detail: 'This pairing request has expired — ask them to send a new one.',
    })).toBe('This pairing request has expired — ask them to send a new one.')
  })

  it('explains that publishing a public space to a GFS is a tier change', () => {
    // The space settings' Publish button on a public space answers this
    // since public stopped meaning "may be on a GFS".
    expect(apiErrorMessage(409, '/api/spaces/s1/publish/g1', {
      code: 'TIER_CHANGE_REQUIRED',
      detail: 'A public space is only visible to connected households.',
    })).toBe(
      "A public space is only visible to households you're paired with. "
      + 'Make it global to list it on a GFS.',
    )
  })

  it('translates the GFS choice refusals by reason', () => {
    expect(apiErrorMessage(422, '/api/spaces', {
      code: 'GFS_SELECTION_REQUIRED', detail: 'x',
    })).toBe('Choose at least one GFS to list this global space on.')
    expect(apiErrorMessage(422, '/api/spaces/s1/gfs-servers', {
      code: 'GFS_SELECTION_INVALID', detail: 'x', params: { reason: 'too_many', max: 8 },
    })).toBe('A space can be listed on at most 8 GFSs.')
    expect(apiErrorMessage(422, '/api/spaces/s1/gfs-servers', {
      code: 'GFS_SELECTION_INVALID', detail: 'x', params: { reason: 'unknown' },
    })).toBe("That GFS isn't connected anymore. Choose one of your active GFS connections.")
    expect(apiErrorMessage(422, '/api/spaces', {
      code: 'GFS_SELECTION_INVALID', detail: 'x', params: { reason: 'not_global' },
    })).toBe('Only a global space is listed on GFSs.')
    // A malformed body keeps the server's detail.
    expect(apiErrorMessage(422, '/api/spaces/s1/gfs-servers', {
      code: 'GFS_SELECTION_INVALID', detail: "That GFS choice can't be used.",
      params: { reason: 'malformed' },
    })).toBe("That GFS choice can't be used.")
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

  it('translates the zone codes, for space and household zones alike', () => {
    expect(apiErrorMessage(409, '/api/household/zones', {
      code: 'ZONE_LIMIT', detail: 'x', params: { max: 50 },
    })).toBe('You have reached the limit of 50 zones. Delete one to add another.')
    // A space zone's ZONE_LIMIT carries no params — the cap is the same.
    expect(apiErrorMessage(409, '/api/spaces/s/zones', {
      code: 'ZONE_LIMIT', detail: 'x',
    })).toBe('You have reached the limit of 50 zones. Delete one to add another.')
    expect(apiErrorMessage(409, '/api/household/zones', {
      code: 'ZONE_NAME_TAKEN', detail: 'x',
    })).toBe('A zone with this name already exists.')
    expect(apiErrorMessage(409, '/api/household/zones/z', {
      code: 'ZONE_READ_ONLY', detail: 'x',
    })).toBe('This zone comes from Home Assistant. Change it in Home Assistant.')
    expect(apiErrorMessage(409, '/api/household/zones/z', {
      code: 'ZONE_IS_HOME', detail: 'x',
    })).toBe("The Home zone can't be removed. Mark another zone as Home first.")
  })

  it('translates the Home Assistant wall-panel codes', () => {
    const at = '/api/admin/dashboard/displays/ha/dev/connect'
    expect(apiErrorMessage(409, at, {
      code: 'DASHBOARD_ADDRESS_REQUIRED', detail: 'x', params: { reason: 'port_not_published' },
    })).toBe("Turn on the dashboard port in the Home Assistant App's Network settings first.")
    expect(apiErrorMessage(409, at, {
      code: 'DASHBOARD_ADDRESS_REQUIRED', detail: 'x', params: { reason: 'no_address' },
    })).toBe('Set the dashboard address first, so the panel can reach the dashboard.')
    expect(apiErrorMessage(502, at, { code: 'DISPLAY_DISCOVERY_FAILED', detail: 'x' }))
      .toBe("Home Assistant didn't answer. Try again in a moment.")
    expect(apiErrorMessage(409, at, { code: 'DISPLAY_PANEL_OFFLINE', detail: 'x' }))
      .toBe('The panel is offline in Home Assistant. Turn it on and try again.')
    expect(apiErrorMessage(409, at, { code: 'DISPLAY_PANEL_NEEDS_UPDATE', detail: 'x' }))
      .toBe("Update the panel's firmware, then try again.")
    expect(apiErrorMessage(404, at, { code: 'DISPLAY_PANEL_NOT_FOUND', detail: 'x' }))
      .toBe("That panel isn't in Home Assistant any more.")
    expect(apiErrorMessage(404, at, { code: 'DISPLAY_DISCOVERY_UNAVAILABLE', detail: 'x' }))
      .toBe("This installation can't find wall panels in Home Assistant.")
  })

  it('translates the wall-panel pairing codes, with their params', () => {
    expect(apiErrorMessage(409, '/api/admin/dashboard/displays', {
      code: 'DASHBOARD_DISABLED', detail: 'x',
    })).toBe('Turn the dashboard on first.')
    expect(apiErrorMessage(404, '/api/admin/dashboard/displays', {
      code: 'DASHBOARD_PAIRING_CODE_INVALID', detail: 'x',
    })).toMatch(/^No panel is waiting with that code\./)
    expect(apiErrorMessage(429, '/api/admin/dashboard/displays', {
      code: 'DASHBOARD_PAIRING_THROTTLED', detail: 'x', params: { retry_after: 61 },
    })).toBe('Too many wrong codes. Try again in 2 min.')
    expect(apiErrorMessage(404, '/api/admin/dashboard/displays/d1', {
      code: 'DASHBOARD_DISPLAY_NOT_FOUND', detail: 'x',
    })).toBe("That panel isn't paired any more.")
    expect(apiErrorMessage(409, '/api/admin/dashboard/displays', {
      code: 'DASHBOARD_DISPLAY_LIMIT', detail: 'x', params: { max: 20 },
    })).toBe('You can pair up to 20 panels. Remove one first.')
  })

  it('translates the household dashboard codes, with their params', () => {
    expect(apiErrorMessage(409, '/api/admin/dashboard', {
      code: 'DASHBOARD_PORT_IN_USE', detail: 'x', params: { port: 8100 },
    })).toBe('Port 8100 is in use by another program.')
    expect(apiErrorMessage(409, '/api/admin/dashboard/setup-link', {
      code: 'DASHBOARD_NOT_RUNNING', detail: 'x',
    })).toBe('Turn the dashboard on before creating a sign-in link.')
    expect(apiErrorMessage(422, '/api/admin/dashboard/pin', {
      code: 'DASHBOARD_PIN_INVALID', detail: 'x', params: { min: 4, max: 6 },
    })).toBe('The PIN must be 4 to 6 digits.')
    expect(apiErrorMessage(422, '/api/admin/dashboard/password', {
      code: 'DASHBOARD_PASSWORD_TOO_SHORT', detail: 'x', params: { min: 8 },
    })).toBe('The dashboard password needs at least 8 characters.')
    expect(apiErrorMessage(422, '/api/admin/dashboard', {
      code: 'DASHBOARD_PASSWORD_REQUIRED', detail: 'x',
    })).toBe('Set a dashboard password before switching the dashboard on.')
    expect(apiErrorMessage(422, '/api/admin/dashboard', {
      code: 'DASHBOARD_PORT_FIXED', detail: 'x',
    })).toContain('Home Assistant App')
    expect(apiErrorMessage(422, '/api/admin/dashboard', {
      code: 'DASHBOARD_SETTINGS_INVALID', detail: 'x', params: { field: 'port' },
    })).toBe("That dashboard setting isn't valid.")
    // Without the port param the server's sentence stays.
    expect(apiErrorMessage(409, '/x', { code: 'DASHBOARD_PORT_IN_USE', detail: 'busy' }))
      .toBe('busy')
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
    [422, 'GFS_PROOF_FAILED', "The server at this address couldn't prove it is the GFS it claims to be. Check the address."],
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

  it('local communities are worded for whoever reads them', () => {
    const msg = (code: string, whose?: string) =>
      apiErrorMessage(403, '/x', { code, detail: 'x', ...(whose ? { params: { whose } } : {}) })
    expect(msg('HOME_NOT_SET', 'own')).toBe(
      'This space is a local community, and your household has no home location yet.')
    expect(msg('LOCATION_REQUIRED', 'host')).toBe(
      'This space is a local community. Its household needs your approximate location (about 1 km) with the request.')
    // Near the edge: worded like any request — the person learns only that.
    expect(msg('JOIN_REQUEST_SENT')).toBe('Your request was sent to the admins.')
    // The pre-rework code is gone: a stale server's detail shows as is.
    expect(msg('HOME_NOT_SHARED', 'own')).toBe('x')
    expect(msg('OUTSIDE_AREA', 'own')).toBe(
      "This space is a local community, and your household's home is outside its area.")
    expect(msg('OUTSIDE_AREA', 'host')).toBe(msg('OUTSIDE_AREA', 'own'))
    expect(msg('OUTSIDE_AREA', 'peer')).toBe("That household's home is outside this local community's area.")
    // Every join a person starts counts, successful ones too — not only
    // "outside" refusals — so the wording says "tried", not "refused".
    expect(msg('LOCAL_AREA_RATE_LIMITED', 'host')).toBe(
      "You've already tried to join this local community a few times today. Try again tomorrow.")
    expect(msg('LOCAL_AREA_RATE_LIMITED', 'own')).toBe(msg('LOCAL_AREA_RATE_LIMITED', 'host'))
    expect(msg('LOCAL_AREA_RATE_LIMITED', 'peer')).toBe(
      'That household has already tried to join this local community a few times today. Try again tomorrow.')
    expect(msg('LOCAL_AREA_PUBLIC_ONLY')).toBe('Only a public space can be a local community.')
    expect(msg('LOCAL_AREA_UNAVAILABLE')).toBe('Only the household hosting this space can change its area.')
    expect(msg('LOCAL_AREA_DELEGATED')).toContain('Let admins act for me')
  })
})
