/**
 * The one level picker for spaces and chats (D9): Everything / Replies and
 * mentions / Nothing; Nothing asks "For how long?"; a running mute says
 * until when and what comes back, with Undo.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'
import { ASK_LENGTHS, NotifLevelPicker, levelLabel, quietLabel } from './NotifLevelPicker'
import type { NotifLevel } from '@/store/notifSettings'

const LEVELS: readonly NotifLevel[] = ['all', 'mentions', 'muted']

function picker(over: Partial<Parameters<typeof NotifLevelPicker>[0]> = {}) {
  const onPick = vi.fn().mockResolvedValue(true)
  const view = render(
    <NotifLevelPicker
      legend="Posts"
      levels={LEVELS}
      level="all"
      mutedUntil={null}
      thenLevel={null}
      hint={(l) => `hint ${l}`}
      onPick={onPick}
      {...over}
    />,
  )
  return { ...view, onPick }
}

describe('levelLabel / quietLabel', () => {
  it('one vocabulary', () => {
    expect(LEVELS.map(levelLabel)).toEqual(['Everything', 'Replies and mentions', 'Nothing'])
  })

  it('says until when and what comes back', () => {
    expect(quietLabel('9999-12-31T23:59:59+00:00', 'all')).toBe('Quiet until you turn it back on')
    expect(quietLabel(null, null)).toBe('Quiet until you turn it back on')
    const soon = new Date(Date.now() + 3600e3).toISOString()
    expect(quietLabel(soon, 'mentions')).toMatch(/^Quiet until .+, then back to Replies and mentions$/)
    expect(quietLabel(soon, null)).toMatch(/^Quiet until .+$/)
  })

  it('asks the lengths in the order D9 names them', () => {
    expect(ASK_LENGTHS).toEqual(['forever', '1h', '8h', '1w'])
  })
})

describe('NotifLevelPicker', () => {
  it('a radio per level, the default tagged, a level saves at once', async () => {
    const { getByRole, getAllByText, onPick } = picker({ defaultLevel: 'mentions' })
    expect(getByRole('group', { name: 'Posts' })).toBeTruthy()
    expect((getByRole('radio', { name: /^Everything/ }) as HTMLInputElement).checked).toBe(true)
    expect(getAllByText('Default')).toHaveLength(1)
    fireEvent.click(getByRole('radio', { name: /^Replies and mentions/ }))
    await waitFor(() => expect(onPick).toHaveBeenCalledWith('mentions', undefined))
  })

  it('Nothing waits for a length; picking another level drops the question', async () => {
    const { getByRole, queryByRole, onPick } = picker()
    fireEvent.click(getByRole('radio', { name: /^Nothing/ }))
    expect(onPick).not.toHaveBeenCalled()
    const ask = getByRole('group', { name: 'For how long?' })
    expect(Array.from(ask.querySelectorAll('button')).map(b => b.textContent)).toEqual([
      'Until I turn it back on', '1 hour', '8 hours', '1 week',
    ])
    fireEvent.click(getByRole('radio', { name: /^Everything/ }))
    expect(queryByRole('group', { name: 'For how long?' })).toBeNull()
    expect(onPick).not.toHaveBeenCalled() // already Everything
    fireEvent.click(getByRole('radio', { name: /^Nothing/ }))
    fireEvent.click(getByRole('button', { name: '1 week' }))
    await waitFor(() => expect(onPick).toHaveBeenCalledWith('muted', '1w'))
    await waitFor(() => expect(queryByRole('group', { name: 'For how long?' })).toBeNull())
  })

  it('a failed length keeps the question open', async () => {
    const { getByRole, onPick } = picker()
    onPick.mockResolvedValue(false)
    fireEvent.click(getByRole('radio', { name: /^Nothing/ }))
    fireEvent.click(getByRole('button', { name: '1 hour' }))
    await waitFor(() => expect(onPick).toHaveBeenCalled())
    expect(getByRole('group', { name: 'For how long?' })).toBeTruthy()
  })

  it('muted: the state line and Undo back to the level from before', async () => {
    const until = new Date(Date.now() + 3600e3).toISOString()
    const { getByRole, onPick } = picker({ level: 'muted', mutedUntil: until, thenLevel: 'mentions' })
    expect(getByRole('status').textContent).toMatch(/then back to Replies and mentions/)
    fireEvent.click(getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(onPick).toHaveBeenCalledWith('mentions', undefined))
  })

  it('muted with nothing to come back to: Undo goes to the default', async () => {
    const { getByRole, onPick } = picker({ level: 'muted', defaultLevel: 'mentions' })
    expect(getByRole('status').textContent).toMatch(/Quiet until you turn it back on/)
    fireEvent.click(getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(onPick).toHaveBeenCalledWith('mentions', undefined))
  })

  it('a hidden legend still names the group', () => {
    const { getByRole } = picker({ legendHidden: true })
    const legend = getByRole('group', { name: 'Posts' }).querySelector('legend')!
    expect(legend.className).toBe('sr-only')
  })
})
