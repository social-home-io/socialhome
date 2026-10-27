import { describe, expect, it } from 'vitest'
import { humanizeViewerError } from './viewer_errors'
import { LOCALES } from './viewer_i18n'

const UNAVAILABLE_COPY = LOCALES.en['error.unavailable']

describe('humanizeViewerError', () => {
  it('maps the uniform GFS 503 to neutral "not available" copy', () => {
    expect(humanizeViewerError('HTTP 503')).toBe(UNAVAILABLE_COPY)
    expect(UNAVAILABLE_COPY).toBe('This isn’t available right now.')
  })

  it('never claims the author is offline or the item is gone', () => {
    // The GFS deliberately can't tell these apart; the copy must not either.
    for (const status of [404, 410, 503]) {
      const copy = humanizeViewerError(`HTTP ${status}`)
      expect(copy).toBe(UNAVAILABLE_COPY)
      expect(copy.toLowerCase()).not.toMatch(/offline|online|ended|isn’t sharing/)
    }
  })

  it('keeps the rate-limit and backpressure copy', () => {
    expect(humanizeViewerError('HTTP 429')).toBe('Too many viewers — try again in a minute.')
    expect(humanizeViewerError('backpressure')).toBe('Too many viewers — try again in a minute.')
  })

  it('falls back to a generic message when there is none', () => {
    expect(humanizeViewerError(null)).toBe('Couldn’t connect.')
    expect(humanizeViewerError('')).toBe('Couldn’t connect.')
  })

  it('says "not available" in every language, never "offline"', () => {
    for (const table of Object.values(LOCALES)) {
      expect(table['error.unavailable'].toLowerCase()).not.toMatch(/offline|online|ended|beendet|hors ligne|desconect/)
    }
  })

  it('says "couldn’t connect" when the network failed, never the browser’s words', () => {
    for (const raw of [
      'Failed to fetch', // Chromium
      'NetworkError when attempting to fetch resource.', // Firefox
      'Load failed', // Safari
      'The operation timed out.',
      'HTTP 408',
    ]) {
      expect(humanizeViewerError(raw)).toBe('Couldn’t connect.')
    }
  })

  it('never shows raw error text: anything else is the one neutral message', () => {
    for (const raw of ['HTTP 500', 'HTTP 502', 'HTTP 400', 'HTTP 403', 'decrypt failed', 'TypeError: x is undefined']) {
      expect(humanizeViewerError(raw)).toBe(UNAVAILABLE_COPY)
    }
  })

  it('only ever answers with a translated message', () => {
    const known = new Set<string>(Object.values(LOCALES.en))
    for (const raw of [null, '', 'HTTP 503', 'HTTP 500', 'HTTP 429', 'Failed to fetch', 'boom']) {
      expect(known.has(humanizeViewerError(raw))).toBe(true)
    }
  })
})
