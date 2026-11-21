import { describe, it, expect } from 'vitest'
import {
  parseSpaceSync, syncChipLabel, syncLastOkLine, syncStateBody, syncStateLabel, syncWaitingLine,
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
})
