/**
 * SpaceSetupCard: presets are real radios in a fieldset, chips are
 * toggle buttons, Done PATCHes exactly the chips and clears the flag,
 * Skip only clears it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/preact'

const patchSpace = vi.fn(async (..._a: unknown[]) => 'saved' as const)
vi.mock('./settings/spacePatch', () => ({ patchSpace: (...a: unknown[]) => patchSpace(...a) }))
const clear = vi.fn(async (_id: string) => {})
vi.mock('./spaceSetup', async (orig) => ({
  ...(await orig<object>()),
  clearSetupPending: (id: string) => clear(id),
}))

const { SpaceSetupCard } = await import('./SpaceSetupCard')

const NEW = {
  chat: true, calendar: true, todo: false, pages: false, gallery: false,
  stickies: false, bazaar: false, location: false, timetable: false,
}

function mount(name = 'Untitled') {
  const onDone = vi.fn()
  const r = render(<SpaceSetupCard spaceId="s1" name={name} features={NEW} onDone={onDone} />)
  return { r, onDone }
}

beforeEach(() => { patchSpace.mockClear(); clear.mockClear() })
afterEach(cleanup)

describe('SpaceSetupCard', () => {
  it('asks "What’s <name> for?" with a radio group of four presets, none picked without a hint', () => {
    const { r } = mount()
    expect(r.getByRole('heading', { name: 'What’s Untitled for?' })).toBeTruthy()
    const group = r.getByRole('group', { name: 'Start from' })
    expect(group.tagName).toBe('FIELDSET')
    const radios = within(group).getAllByRole('radio') as HTMLInputElement[]
    expect(radios).toHaveLength(4)
    expect(radios.every((x) => !x.checked)).toBe(true)
    expect(r.getByRole('radio', { name: /Just a feed/ })).toBeTruthy()
    // The chips start from the space's own sections (feed + chat + calendar).
    const pressed = r.getAllByRole('button', { pressed: true }).map((b) => b.textContent?.trim())
    expect(pressed).toEqual(['✓ Chat', '✓ Calendar'])
  })

  it('guesses from the name: "Book club" starts on Club or group', () => {
    const { r } = mount('Book club')
    expect((r.getByRole('radio', { name: /Club or group/ }) as HTMLInputElement).checked).toBe(true)
    const pressed = r.getAllByRole('button', { pressed: true }).map((b) => b.textContent?.trim())
    expect(pressed).toEqual(['✓ Chat', '✓ Calendar', '✓ Pages'])
  })

  it('a preset sets the chips; a chip flips on its own; Done saves exactly that', async () => {
    const { r, onDone } = mount()
    fireEvent.click(r.getByRole('radio', { name: /Family/ }))
    fireEvent.click(r.getByRole('button', { name: /Stickies/ }))
    fireEvent.click(r.getByRole('button', { name: /Chat/ }))
    fireEvent.click(r.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(patchSpace).toHaveBeenCalledWith('s1', {
      features: { ...NEW, chat: false, todo: true, gallery: true, stickies: true },
    }, expect.anything())
    expect(clear).toHaveBeenCalledWith('s1')
  })

  it('"Just a feed" turns every section off', async () => {
    const { r } = mount('Family')
    fireEvent.click(r.getByRole('radio', { name: /Just a feed/ }))
    expect(r.queryAllByRole('button', { pressed: true })).toEqual([])
  })

  it('Skip keeps the space as it is and never asks again', async () => {
    const { r, onDone } = mount()
    fireEvent.click(r.getByRole('button', { name: 'Skip' }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(patchSpace).not.toHaveBeenCalled()
    expect(clear).toHaveBeenCalledWith('s1')
  })

  it('a refused save keeps the card (and the flag)', async () => {
    patchSpace.mockResolvedValueOnce('failed' as never)
    const { r, onDone } = mount()
    fireEvent.click(r.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(patchSpace).toHaveBeenCalled())
    expect(onDone).not.toHaveBeenCalled()
    expect(clear).not.toHaveBeenCalled()
  })
})
