/**
 * DrawResultSheet — Social Home's own gift-draw result sheet (v_78).
 *
 * Pinned here: the result's words ("You're giving a gift to Ben (Familie
 * Okafor)", the wish list, budget, exchange day, "Only you can see this"),
 * the tap-jacking guard (nothing closes it in its first 600 ms), the
 * hand-over and redraw notes, and the empty / failed states.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const h = vi.hoisted(() => ({ getDrawResult: vi.fn() }))

vi.mock('./drawBridge', () => ({ getDrawResult: h.getDrawResult }))

import { ApiError } from '@/api'
import { DrawResultSheet, DONE_ARM_MS, dayLabel } from './DrawResultSheet'

const BEN = {
  app_id: 'gift-draw',
  session_id: 't1',
  name: 'Ben Okafor',
  household: 'Familie Okafor',
  wish: 'Anything with dinosaurs',
  wish_updated: false,
  draw_name: 'Wichteln 2026',
  budget: 'about 20 €',
  day: '2026-12-24',
  again: 0,
  handed_over_by: null,
}

async function open(result: Record<string, unknown> = BEN) {
  h.getDrawResult.mockResolvedValue(result)
  const onClose = vi.fn()
  const view = render(<DrawResultSheet appId="gift-draw" sessionId="t1" onClose={onClose} />)
  await view.findByText(String(result.name))
  return { ...view, onClose }
}

describe('DrawResultSheet', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-12-01T12:00:00Z'))
    h.getDrawResult.mockReset()
  })
  afterEach(() => { vi.useRealTimers() })

  it('says whom you give to, their wish list, the budget and the day', async () => {
    const { getByRole, getByText } = await open()
    const dialog = getByRole('dialog')
    expect(dialog.textContent).toContain('Social Home · Gift draw')
    expect(getByText("You're giving a gift to")).toBeTruthy()
    expect(getByText('Familie Okafor')).toBeTruthy()
    expect(getByText("Ben Okafor's wish list")).toBeTruthy()
    expect(getByText('Anything with dinosaurs')).toBeTruthy()
    expect(getByText('about 20 €')).toBeTruthy()
    expect(dialog.textContent).toContain('24')
    expect(dialog.textContent).toContain("Only you can see this. Ben Okafor won't know it's you.")
    expect(h.getDrawResult).toHaveBeenCalledWith('gift-draw', 't1')
  })

  it('ignores Done in its first 600 ms (an app-timed tap)', async () => {
    const { getByText, onClose } = await open()
    vi.setSystemTime(Date.now() + DONE_ARM_MS - 1)
    fireEvent.click(getByText('Done'))
    expect(onClose).not.toHaveBeenCalled()
    vi.setSystemTime(Date.now() + 1)
    fireEvent.click(getByText('Done'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('ignores Escape in its first 600 ms too', async () => {
    const { onClose } = await open()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    vi.setSystemTime(Date.now() + DONE_ARM_MS)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('names a hand-over, an update and a redraw', async () => {
    const { getByRole } = await open({ ...BEN, handed_over_by: 'Cleo', wish_updated: true, again: 1 })
    const text = getByRole('dialog').textContent ?? ''
    expect(text).toContain("Cleo left the draw — you're now giving to Ben Okafor")
    expect(text).toContain('updated')
    expect(text).toContain('Wichteln 2026 · draw 2')
  })

  it('an empty wish list says so', async () => {
    const { getByText } = await open({ ...BEN, wish: '', budget: '', day: null })
    expect(getByText("Ben Okafor hasn't written a wish list.")).toBeTruthy()
  })

  it('no result yet — and the heading doesn\'t promise one', async () => {
    h.getDrawResult.mockRejectedValue(new ApiError(404, 'x', { code: 'APP_DRAW_NO_RESULT' }))
    const { findByText, getByRole } = render(<DrawResultSheet appId="gift-draw" sessionId="t1" onClose={() => {}} />)
    expect(await findByText("There's no name for you in this gift draw yet.")).toBeTruthy()
    expect(getByRole('dialog').textContent).not.toContain('Your name is ready')
    expect(getByRole('dialog').textContent).toContain('Gift draw')
  })

  it('a failed load offers Try again', async () => {
    h.getDrawResult.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(BEN)
    const { findByText, getByText } = render(<DrawResultSheet appId="gift-draw" sessionId="t1" onClose={() => {}} />)
    expect(await findByText("Couldn't load your gift draw.")).toBeTruthy()
    fireEvent.click(getByText('Try again'))
    await waitFor(() => expect(getByText('Ben Okafor')).toBeTruthy())
  })
})

describe('dayLabel', () => {
  it('reads the day in the reader\'s language, whatever their time zone', () => {
    expect(dayLabel('2026-12-24', 'en')).toMatch(/24/)
    expect(dayLabel('2026-12-24', 'de')).toMatch(/Dezember/)
    expect(dayLabel('not-a-day', 'en')).toBe('not-a-day')
  })
})
