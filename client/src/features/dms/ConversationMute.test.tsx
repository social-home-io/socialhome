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


describe('group notification level (§23.42)', () => {
  it('header menu offers All / Only @mentions above the lengths; a pick saves it', async () => {
    put.mockResolvedValue({ level: 'mentions' })
    const onLevel = vi.fn()
    const { getByLabelText, getAllByRole, queryByRole } = render(
      <MuteButton
        convId="g1" mutedUntil={null} onChange={vi.fn()}
        level="all" onLevelChange={onLevel}
      />,
    )
    fireEvent.click(getByLabelText('Mute notifications'))
    const radios = getAllByRole('menuitemradio')
    expect(radios.map(r => [r.textContent, r.getAttribute('aria-checked')])).toEqual([
      ['✓All messages', 'true'],
      ['Only @mentions', 'false'],
    ])
    // Focus lands on the first choice; the mute lengths are still there.
    expect(document.activeElement).toBe(radios[0])
    expect(getAllByRole('menuitem')).toHaveLength(4)
    fireEvent.click(radios[1])
    await waitFor(() => expect(onLevel).toHaveBeenCalledWith('mentions'))
    expect(put).toHaveBeenCalledWith('/api/conversations/g1/notif-prefs', { level: 'mentions' })
    expect(queryByRole('menu')).toBeNull()
    expect(toast).toHaveBeenCalledWith(expect.stringMatching(/@mentions you/), 'success')
  })

  it('a mentions-only group shows the @ badge and says so on the bell', () => {
    const { getByLabelText, container } = render(
      <MuteButton
        convId="g1" mutedUntil={null} onChange={vi.fn()}
        level="mentions" onLevelChange={vi.fn()}
      />,
    )
    const bell = getByLabelText(/only @mentions/)
    expect(bell.classList.contains('sh-thread-mute-btn--mentions')).toBe(true)
    expect(container.querySelector('.sh-thread-mute-btn__at')?.textContent).toBe('@')
  })

  it('a failed save keeps the level and toasts an error', async () => {
    put.mockRejectedValue(new Error('boom'))
    const onLevel = vi.fn()
    const { getByLabelText, getAllByRole } = render(
      <MuteButton
        convId="g1" mutedUntil={null} onChange={vi.fn()}
        level="all" onLevelChange={onLevel}
      />,
    )
    fireEvent.click(getByLabelText('Mute notifications'))
    fireEvent.click(getAllByRole('menuitemradio')[1])
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.any(String), 'error'))
    expect(onLevel).not.toHaveBeenCalled()
  })

  it('Group info section: radios switch the level and the state line follows', async () => {
    put.mockResolvedValue({ level: 'mentions' })
    const onLevel = vi.fn()
    const { getByLabelText, getByText, rerender } = render(
      <MuteSection
        convId="g1" mutedUntil={null} onChange={vi.fn()}
        level="all" onLevelChange={onLevel}
      />,
    )
    const only = getByLabelText('Only @mentions') as HTMLInputElement
    expect((getByLabelText('All messages') as HTMLInputElement).checked).toBe(true)
    fireEvent.click(only)
    await waitFor(() => expect(onLevel).toHaveBeenCalledWith('mentions'))
    rerender(
      <MuteSection
        convId="g1" mutedUntil={null} onChange={vi.fn()}
        level="mentions" onLevelChange={onLevel}
      />,
    )
    expect(getByText(/notified when someone @mentions you/)).toBeTruthy()
  })

  it('1:1 surfaces (no level props) show no level choice', () => {
    const { getByLabelText, queryAllByRole } = render(
      <MuteButton convId="d1" mutedUntil={null} onChange={vi.fn()} />,
    )
    fireEvent.click(getByLabelText('Mute notifications'))
    expect(queryAllByRole('menuitemradio')).toHaveLength(0)
  })
})
