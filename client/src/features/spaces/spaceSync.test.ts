import { describe, it, expect } from 'vitest'
import {
  parseSpaceSync, syncBody, syncChipLabel, syncHeading, syncLastOkLine, syncStateBody,
  syncStateLabel, syncWaitingLine,
} from './spaceSync'

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

describe('spaceSync', () => {
  it('parses the wire shape and refuses anything else', () => {
    expect(parseSpaceSync({ state: 'behind', last_ok_at: 'x', waiting_to_send: 2 }))
      .toEqual({ state: 'behind', last_ok_at: 'x', waiting_to_send: 2 })
    expect(parseSpaceSync({ state: 'never' }))
      .toEqual({ state: 'never', last_ok_at: null, waiting_to_send: 0 })
    expect(parseSpaceSync({ state: 'behind', waiting_to_send: -3 })?.waiting_to_send).toBe(0)
    expect(parseSpaceSync({ state: 'weird' })).toBeNull()
    expect(parseSpaceSync(null)).toBeNull()
    expect(parseSpaceSync('up_to_date')).toBeNull()
  })

  it('no chip when up to date with nothing waiting', () => {
    expect(syncChipLabel({ state: 'up_to_date', last_ok_at: ago(1), waiting_to_send: 0 })).toBeNull()
    expect(syncChipLabel(null)).toBeNull()
  })

  it('behind without a known last ok still says behind', () => {
    expect(syncChipLabel({ state: 'behind', last_ok_at: null, waiting_to_send: 0 })).toBe('Behind')
  })

  it('words each state for the settings', () => {
    expect(syncStateLabel('up_to_date')).toBe('Up to date')
    expect(syncStateLabel('catching_up')).toBe('Catching up…')
    expect(syncStateLabel('behind')).toBe('Behind')
    expect(syncStateLabel('host_offline')).toBe('Host offline')
    expect(syncStateLabel('never')).toBe('Not synced yet')
    expect(syncStateBody('host_offline', false)).toContain("isn't answering")
    expect(syncStateBody('behind', false)).toContain('every 30 minutes')
    expect(syncStateBody('up_to_date', true)).toContain('hosts the space')
  })

  it('the facts lines', () => {
    expect(syncLastOkLine({ state: 'behind', last_ok_at: ago(5), waiting_to_send: 0 }))
      .toBe('Last up to date 5 min ago')
    expect(syncLastOkLine({ state: 'never', last_ok_at: null, waiting_to_send: 0 })).toBeNull()
    expect(syncWaitingLine({ state: 'never', last_ok_at: null, waiting_to_send: 0 })).toBeNull()
    expect(syncWaitingLine({ state: 'never', last_ok_at: null, waiting_to_send: 4 }))
      .toBe('4 waiting to send')
  })

  describe('signed sync page (v_95)', () => {
    const page = (match: boolean | null, differing: number | null = null) => ({
      seq: 812, at: ago(3), match, differing_buckets: differing,
    })

    it('parses the page and drops a malformed one', () => {
      expect(parseSpaceSync({ state: 'up_to_date', page: page(true, 0) })?.page)
        .toEqual({ seq: 812, at: expect.any(String), match: true, differing_buckets: 0 })
      expect(parseSpaceSync({ state: 'up_to_date', page: { seq: 'x' } })?.page).toBeUndefined()
      expect(parseSpaceSync({ state: 'up_to_date' })).not.toHaveProperty('page')
    })

    it('a match reads up to date, checked against page N', () => {
      const sync = { state: 'up_to_date' as const, last_ok_at: null, waiting_to_send: 0, page: page(true, 0) }
      expect(syncHeading(sync)).toBe('Up to date · checked against page 812')
      expect(syncChipLabel(sync)).toBeNull()
    })

    it('a mismatch reads needs sync with the parts that differ', () => {
      const sync = { state: 'behind' as const, last_ok_at: ago(5), waiting_to_send: 0, page: page(false, 3) }
      expect(syncChipLabel(sync)).toBe('Needs sync · 3 parts differ')
      expect(syncHeading(sync)).toBe('Needs sync')
      expect(syncBody(sync, false)).toContain('signed')
      expect(syncChipLabel({ ...sync, page: page(false, 1) })).toBe('Needs sync · 1 part differs')
    })

    it('without a compared page the answer-based words stay', () => {
      const sync = { state: 'behind' as const, last_ok_at: null, waiting_to_send: 0, page: page(null) }
      expect(syncChipLabel(sync)).toBe('Behind')
      expect(syncHeading(sync)).toBe('Behind')
      expect(syncBody(sync, false)).toContain('every 30 minutes')
    })
  })
})
