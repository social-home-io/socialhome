import { describe, it, expect } from 'vitest'
import { ApiError } from '@/api'
import { FINAL_GFS_ERRORS, gfsConnectErrorText } from './gfsErrors'

const PATH = '/api/gfs/connections'

describe('gfsConnectErrorText', () => {
  it.each([
    [502, 'GFS_UNREACHABLE', /Couldn't reach the GFS/],
    [409, 'GFS_SIGNUP_CLOSED', /isn't taking sign-ups/],
    [503, 'GFS_BUSY', /The GFS is busy/],
    [422, 'GFS_IDENTITY_MISMATCH', /doesn't look like the Social Home GFS/],
    [422, 'GFS_PAIRING_FAILED', /didn't accept this household/],
    [409, 'ALREADY_CONNECTED', /already connected to this GFS/],
    [429, 'RATE_LIMITED', /Too many tries/],
  ])('%i %s → its own translated line, not the household-pairing one', (status, code, text) => {
    const msg = gfsConnectErrorText(new ApiError(status, PATH, { code, detail: 'server words' }))
    expect(msg).toMatch(text)
    expect(msg).not.toMatch(/looks malformed|already paired|server hit an error/)
  })

  it('a 422 never reads as "the pairing code looks malformed"', () => {
    const msg = gfsConnectErrorText(new ApiError(422, PATH, { code: 'GFS_IDENTITY_MISMATCH', detail: 'x' }))
    expect(msg).not.toMatch(/malformed/)
  })

  it('an unknown, specific refusal keeps the server reason', () => {
    const msg = gfsConnectErrorText(new ApiError(422, PATH, { code: 'GFS_TOKEN_EXPIRED', detail: 'That code has expired.' }))
    expect(msg).toBe('That code has expired.')
  })

  it('a 502 without a code is the generic server line, not a pairing one', () => {
    expect(gfsConnectErrorText(new ApiError(502, PATH, null))).toMatch(/Something went wrong on the server/)
  })

  it.each([401, 403])('%i says only admins can connect', (status) => {
    expect(gfsConnectErrorText(new ApiError(status, PATH, { code: 'FORBIDDEN', detail: 'Admin only.' })))
      .toMatch(/household admins/)
  })

  it('a network failure keeps its message without the "Error:" prefix', () => {
    expect(gfsConnectErrorText(new Error('Error: Failed to fetch'))).toBe('Failed to fetch')
  })

  it('anything else gets the GFS generic line', () => {
    expect(gfsConnectErrorText('???')).toMatch(/Couldn't connect to the GFS/)
    expect(gfsConnectErrorText(new Error(''))).toMatch(/Couldn't connect to the GFS/)
  })

  it('names the refusals a retry cannot fix', () => {
    expect([...FINAL_GFS_ERRORS].sort()).toEqual(['GFS_IDENTITY_MISMATCH', 'GFS_SIGNUP_CLOSED'])
  })
})
