import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const get = vi.fn()
const put = vi.fn()
const del = vi.fn()
const toast = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => get(...a),
    put: (...a: unknown[]) => put(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}))
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => toast(...a) }))

import { MuteButton, MuteSection } from './ConversationMute'

beforeEach(() => {
  get.mockReset(); put.mockReset(); del.mockReset(); toast.mockReset()
  get.mockResolvedValue([])
  del.mockResolvedValue(undefined)
})

describe('MuteButton (thread header)', () => {
  it('unmuted: the bell opens a menu of lengths and a pick mutes', async () => {
    put.mockResolvedValue({ muted_until: '2026-09-29T11:00:00+00:00' })
    const onChange = vi.fn()
    const { getByLabelText, getByRole, getAllByRole, queryByRole } = render(
      <MuteButton convId="c1" mutedUntil={null} onChange={onChange} />,
    )
    const trigger = getByLabelText('Mute notifications')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(trigger)
    expect(getByRole('menu')).toBeTruthy()
    const items = getAllByRole('menuitem')
    expect(items.map(i => i.textContent)).toEqual([
      'For 1 hour', 'For 8 hours', 'For 1 week', 'Until I turn it back on',
    ])
    // Focus moves into the menu so a keyboard user can pick straight away.
    expect(document.activeElement).toBe(items[0])
    fireEvent.click(items[0])
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('2026-09-29T11:00:00+00:00'))
    expect(put).toHaveBeenCalledWith('/api/conversations/c1/mute', { duration: '1h' })
    expect(queryByRole('menu')).toBeNull()
    expect(toast).toHaveBeenCalledWith(expect.stringMatching(/^Muted/), 'success')
  })

  it('Escape closes the menu without muting', () => {
    const { getByLabelText, queryByRole } = render(
      <MuteButton convId="c1" mutedUntil={null} onChange={vi.fn()} />,
    )
    fireEvent.click(getByLabelText('Mute notifications'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(queryByRole('menu')).toBeNull()
    expect(put).not.toHaveBeenCalled()
  })

  it('muted: shows the bell-slash and one click unmutes', async () => {
    const onChange = vi.fn()
    const { getByRole } = render(
      <MuteButton convId="c1" mutedUntil="9999-12-31T23:59:59+00:00" onChange={onChange} />,
    )
    const btn = getByRole('button')
    expect(btn.textContent).toBe('🔕')
    expect(btn.getAttribute('aria-label')).toBe('Muted until you turn it back on — select to unmute')
    fireEvent.click(btn)
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(null))
    expect(del).toHaveBeenCalledWith('/api/conversations/c1/mute')
  })

  it('an expired mute shows the plain bell again', () => {
    const { getByLabelText } = render(
      <MuteButton convId="c1" mutedUntil="2020-01-01T00:00:00+00:00" onChange={vi.fn()} />,
    )
    expect(getByLabelText('Mute notifications').textContent).toBe('🔔')
  })

  it('a failed change keeps the state and says so', async () => {
    put.mockRejectedValue(new Error('offline'))
    const onChange = vi.fn()
    const { getByLabelText, getAllByRole, getByRole } = render(
      <MuteButton convId="c1" mutedUntil={null} onChange={onChange} />,
    )
    fireEvent.click(getByLabelText('Mute notifications'))
    fireEvent.click(getAllByRole('menuitem')[3])
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "Couldn't change notifications. Try again.", 'error'))
    expect(onChange).not.toHaveBeenCalled()
    // The menu stays open for another try.
    expect(getByRole('menu')).toBeTruthy()
  })
})

describe('MuteSection (Group info)', () => {
  it('offers the four lengths while unmuted', () => {
    const { getAllByRole } = render(
      <MuteSection convId="g1" mutedUntil={null} onChange={vi.fn()} />,
    )
    expect(getAllByRole('button').map(b => b.textContent)).toEqual([
      'For 1 hour', 'For 8 hours', 'For 1 week', 'Until I turn it back on',
    ])
  })
})
