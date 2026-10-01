import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
    delete: (...args: unknown[]) => apiDelete(...args),
  },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

interface Row {
  id: string; content: string; color: string; position_x: number; position_y: number
  space_id: string | null
}
const sticky = (id: string, over: Partial<Row> = {}): Row => ({
  id, content: `Note ${id}`, color: '#FFF9B1', position_x: 100, position_y: 100,
  space_id: null, ...over,
})

function wireApi(byUrl: Record<string, Row[] | Error>) {
  apiGet.mockImplementation(async (url: string) => {
    const v = byUrl[url]
    if (v instanceof Error) throw v
    return v ?? []
  })
  apiPatch.mockImplementation(async (_url: string, body: object) => ({ ...body }))
  apiDelete.mockResolvedValue(undefined)
}

async function setup(byUrl: Record<string, Row[] | Error>, props: { spaceId?: string } = {}) {
  wireApi(byUrl)
  const tl = await import('@testing-library/preact')
  const mod = await import('./StickyBoardPage')
  const r = tl.render(<mod.default {...props} />)
  return { ...tl, ...r }
}

/** A controllable ResizeObserver: ``resizeBoard(w)`` reports a width. */
type ROCallback = (entries: { contentRect: { width: number } }[]) => void
const observers: ROCallback[] = []
class FakeResizeObserver {
  cb: ROCallback
  constructor(cb: ROCallback) { this.cb = cb; observers.push(cb) }
  observe() {}
  disconnect() { const i = observers.indexOf(this.cb); if (i >= 0) observers.splice(i, 1) }
  unobserve() {}
}
async function resizeBoard(width: number) {
  const { act } = await import('@testing-library/preact')
  act(() => { for (const cb of [...observers]) cb([{ contentRect: { width } }]) })
}

let mm: typeof window.matchMedia
function setNarrow(narrow: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: narrow && q === '(max-width: 639px)', media: q,
    addEventListener() {}, removeEventListener() {},
  })) as unknown as typeof window.matchMedia
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDelete.mockReset()
  mm = window.matchMedia
  setNarrow(false)
  observers.length = 0
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  window.matchMedia = mm
})

describe('StickyBoardPage', () => {
  it('shows the notes with a count, one edit button each and a short label', async () => {
    const long = 'x'.repeat(120)
    const t = await setup({ '/api/stickies': [sticky('a'), sticky('b', { content: long })] })
    await t.findByText('2 notes')
    const edits = t.getAllByRole('button', { name: /^Edit note:/ })
    expect(edits).toHaveLength(2)
    const label = edits[1].getAttribute('aria-label')!
    expect(label.length).toBeLessThan(80)
    expect(label.endsWith('…')).toBe(true)
    // No separate ✎ button.
    expect(t.container.querySelector('.sh-sticky-edit-btn')).toBeNull()
  })

  it('a failed load shows the error with Retry, never the empty state', async () => {
    const t = await setup({ '/api/stickies': new Error('down') })
    await t.findByRole('alert')
    expect(t.queryByText('No notes yet')).toBeNull()
    wireApi({ '/api/stickies': [sticky('a')] })
    t.fireEvent.click(t.getByRole('button', { name: 'Retry' }))
    await t.findByRole('button', { name: 'Edit note: Note a' })
  })

  it('shows a skeleton while loading', async () => {
    apiGet.mockReturnValue(new Promise(() => {}))
    const tl = await import('@testing-library/preact')
    const mod = await import('./StickyBoardPage')
    const r = tl.render(<mod.default />)
    expect(r.container.querySelector('.sh-list-skeleton--sticky')).not.toBeNull()
  })

  it('empty board offers a call to action', async () => {
    const t = await setup({ '/api/stickies': [] })
    await t.findByText('No notes yet')
    expect(t.getByRole('button', { name: 'Add the first note' })).toBeTruthy()
  })

  it('sizes notes relative to the board, not in pixels', async () => {
    const t = await setup({ '/api/stickies': [sticky('a', { position_x: 500, position_y: 350 })] })
    await t.findByRole('button', { name: 'Edit note: Note a' })
    const note = t.container.querySelector('[data-sticky-id="a"]') as HTMLElement
    expect(note.style.width).toBe('')
    expect(note.style.getPropertyValue('--sh-sticky-x')).toBe('50%')
    expect(note.style.getPropertyValue('--sh-sticky-y')).toBe('50%')
  })

  it('arrow keys on the grip move the note and send ONE patch after a pause', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const t = await setup({ '/api/stickies': [sticky('a')] })
    const grip = await t.findByRole('button', { name: 'Move note: Note a' })
    t.fireEvent.keyDown(grip, { key: 'ArrowRight' })
    t.fireEvent.keyDown(grip, { key: 'ArrowRight' })
    t.fireEvent.keyDown(grip, { key: 'ArrowDown', shiftKey: true })
    const note = t.container.querySelector('[data-sticky-id="a"]') as HTMLElement
    expect(note.style.getPropertyValue('--sh-sticky-x')).toBe('12%')
    expect(apiPatch).not.toHaveBeenCalled()
    expect(t.container.querySelector('[aria-live="polite"]')!.textContent)
      .toBe('Moved to 12% across, 21% down')
    await vi.advanceTimersByTimeAsync(600)
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch).toHaveBeenCalledWith('/api/stickies/a', { position_x: 120, position_y: 150 })
  })

  it('blur sends the pending move at once', async () => {
    const t = await setup({ '/api/stickies': [sticky('a')] })
    const grip = await t.findByRole('button', { name: 'Move note: Note a' })
    t.fireEvent.keyDown(grip, { key: 'ArrowLeft' })
    t.fireEvent.blur(grip)
    expect(apiPatch).toHaveBeenCalledWith('/api/stickies/a', { position_x: 90, position_y: 100 })
  })

  it('Escape cancels a pending keyboard move', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const t = await setup({ '/api/stickies': [sticky('a')] })
    const grip = await t.findByRole('button', { name: 'Move note: Note a' })
    t.fireEvent.keyDown(grip, { key: 'ArrowUp' })
    t.fireEvent.keyDown(grip, { key: 'Escape' })
    const note = t.container.querySelector('[data-sticky-id="a"]') as HTMLElement
    expect(note.style.getPropertyValue('--sh-sticky-y')).toBe(`${(100 / 700) * 100}%`)
    await vi.advanceTimersByTimeAsync(600)
    expect(apiPatch).not.toHaveBeenCalled()
    expect(t.container.querySelector('[aria-live="polite"]')!.textContent).toBe('Move cancelled')
  })

  it('keyboard moves stay on the board', async () => {
    const t = await setup({ '/api/stickies': [sticky('a', { position_x: 0, position_y: 0 })] })
    const grip = await t.findByRole('button', { name: 'Move note: Note a' })
    t.fireEvent.keyDown(grip, { key: 'ArrowLeft', shiftKey: true })
    t.fireEvent.keyDown(grip, { key: 'ArrowUp' })
    t.fireEvent.blur(grip)
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('on a phone shows a grid ordered top-to-bottom, left-to-right, without grips', async () => {
    setNarrow(true)
    const t = await setup({
      '/api/stickies': [
        sticky('low', { position_y: 500, position_x: 0 }),
        sticky('right', { position_y: 10, position_x: 600 }),
        sticky('left', { position_y: 10, position_x: 20 }),
        // A few units lower but on the same row as "left": still after it.
        sticky('mid', { position_y: 40, position_x: 300 }),
        sticky('first', { position_y: 30, position_x: 0 }),
      ],
    })
    await t.findByRole('button', { name: 'Edit note: Note low' })
    const ids = [...t.container.querySelectorAll('[data-sticky-id]')]
      .map(n => n.getAttribute('data-sticky-id'))
    expect(ids).toEqual(['first', 'left', 'mid', 'right', 'low'])
    expect(t.container.querySelector('.sh-sticky-grid')).not.toBeNull()
    expect(t.queryByRole('button', { name: /^Move note/ })).toBeNull()
  })

  it('a space board loads its own notes and ignores the household board', async () => {
    const t = await setup({
      '/api/stickies': [sticky('h')],
      '/api/spaces/sp1/stickies': [sticky('s', { space_id: 'sp1' })],
    }, { spaceId: 'sp1' })
    await t.findByRole('button', { name: 'Edit note: Note s' })
    expect(t.queryByRole('button', { name: 'Edit note: Note h' })).toBeNull()
    t.fireEvent.blur(t.getByRole('button', { name: 'Move note: Note s' }))
    t.fireEvent.keyDown(t.getByRole('button', { name: 'Move note: Note s' }), { key: 'ArrowRight' })
    t.fireEvent.blur(t.getByRole('button', { name: 'Move note: Note s' }))
    expect(apiPatch).toHaveBeenCalledWith('/api/spaces/sp1/stickies/s', { position_x: 110, position_y: 100 })
  })

  it('a board narrower than ~760 px shows the grid even on a wide viewport', async () => {
    const t = await setup({ '/api/stickies': [sticky('a'), sticky('b', { position_y: 300 })] })
    await t.findByRole('button', { name: 'Move note: Note a' })
    expect(t.container.querySelector('.sh-sticky-canvas')).not.toBeNull()
    await resizeBoard(700)
    expect(t.container.querySelector('.sh-sticky-grid')).not.toBeNull()
    expect(t.container.querySelector('.sh-sticky-canvas')).toBeNull()
    expect(t.queryByRole('button', { name: /^Move note/ })).toBeNull()
    // Widening back past the threshold (plus a little slack) restores the board.
    await resizeBoard(765)
    expect(t.container.querySelector('.sh-sticky-grid')).not.toBeNull()
    await resizeBoard(900)
    expect(t.container.querySelector('.sh-sticky-canvas')).not.toBeNull()
  })

  it('a phone viewport starts as the grid; a wide measured board switches to the canvas', async () => {
    setNarrow(true)
    const t = await setup({ '/api/stickies': [sticky('a')] })
    await t.findByRole('button', { name: 'Edit note: Note a' })
    expect(t.container.querySelector('.sh-sticky-grid')).not.toBeNull()
    await resizeBoard(1000)
    expect(t.container.querySelector('.sh-sticky-canvas')).not.toBeNull()
  })

  it('switches at 760 px going narrower and at 776 px going wider', async () => {
    const t = await setup({ '/api/stickies': [sticky('a')] })
    await t.findByRole('button', { name: 'Move note: Note a' })
    const mode = () => t.container.querySelector('.sh-sticky-canvas') ? 'canvas' : 'grid'
    await resizeBoard(760)
    expect(mode()).toBe('canvas')
    await resizeBoard(759)
    expect(mode()).toBe('grid')
    await resizeBoard(775)
    expect(mode()).toBe('grid')
    await resizeBoard(776)
    expect(mode()).toBe('canvas')
  })

  it('unmounting sends a pending keyboard move at once', async () => {
    const t = await setup({ '/api/stickies': [sticky('a')] })
    const grip = await t.findByRole('button', { name: 'Move note: Note a' })
    t.fireEvent.keyDown(grip, { key: 'ArrowDown' })
    expect(apiPatch).not.toHaveBeenCalled()
    t.unmount()
    expect(apiPatch).toHaveBeenCalledWith('/api/stickies/a', { position_x: 100, position_y: 110 })
  })

  it('at an edge an arrow toward it does nothing but say so', async () => {
    const t = await setup({ '/api/stickies': [sticky('a', { position_x: 0, position_y: 0 })] })
    const grip = await t.findByRole('button', { name: 'Move note: Note a' })
    const live = () => t.container.querySelector('[aria-live="polite"]')!.textContent!
    t.fireEvent.keyDown(grip, { key: 'ArrowLeft' })
    expect(live()).toBe('Already at the edge')
    // Said again on the next press (a zero-width suffix makes it new text).
    t.fireEvent.keyDown(grip, { key: 'ArrowUp' })
    expect(live().replace(/\u200B/g, '')).toBe('Already at the edge')
    expect(live()).not.toBe('Already at the edge')
    t.fireEvent.blur(grip)
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('a note drawn inside from past the edge moves from where it is drawn', async () => {
    // Stored at x 950: drawn at the right edge (max 820 for an 18 % note).
    const t = await setup({ '/api/stickies': [sticky('a', { position_x: 950 })] })
    const grip = await t.findByRole('button', { name: 'Move note: Note a' })
    t.fireEvent.keyDown(grip, { key: 'ArrowRight' })
    expect(t.container.querySelector('[aria-live="polite"]')!.textContent).toBe('Already at the edge')
    t.fireEvent.keyDown(grip, { key: 'ArrowLeft' })
    t.fireEvent.blur(grip)
    expect(apiPatch).toHaveBeenCalledWith('/api/stickies/a', { position_x: 810, position_y: 100 })
  })

  it('a dark custom colour switches the note to the light ink, on the board and in the grid', async () => {
    const t = await setup({ '/api/stickies': [
      sticky('dark', { color: '#123456' }), sticky('pale'), sticky('bad', { color: 'not-a-colour' }),
    ] })
    await t.findByRole('button', { name: 'Edit note: Note dark' })
    const note = (id: string) => t.container.querySelector(`[data-sticky-id="${id}"]`) as HTMLElement
    expect(note('dark').classList.contains('sh-ink-light')).toBe(true)
    expect(note('pale').classList.contains('sh-ink-light')).toBe(false)
    expect(note('bad').style.background).toContain('255, 249, 177')
    expect(note('bad').classList.contains('sh-ink-light')).toBe(false)
    await resizeBoard(500)
    expect(t.container.querySelector('.sh-sticky-grid')).not.toBeNull()
    expect(note('dark').classList.contains('sh-ink-light')).toBe(true)
    expect(note('bad').style.background).toContain('255, 249, 177')
  })

  it('a zero width (hidden board) is ignored', async () => {
    const t = await setup({ '/api/stickies': [sticky('a')] })
    await t.findByRole('button', { name: 'Move note: Note a' })
    await resizeBoard(0)
    expect(t.container.querySelector('.sh-sticky-canvas')).not.toBeNull()
  })
})

describe('StickyBoardPage grip pointer', () => {
  async function setupWithDialog() {
    wireApi({ '/api/stickies': [sticky('a')] })
    const tl = await import('@testing-library/preact')
    const mod = await import('./StickyBoardPage')
    const dlg = await import('@/components/StickyDialog')
    const r = tl.render(<><mod.default /><dlg.StickyDialog /></>)
    const grip = await tl.findByRole(r.container as HTMLElement, 'button', { name: 'Move note: Note a' })
    return { ...tl, ...r, grip }
  }
  const rect = { left: 0, top: 0, x: 0, y: 0, width: 1000, height: 700, right: 1000, bottom: 700, toJSON() {} }

  beforeEach(() => {
    // The board is 1000 × 700 px at the origin (1 px = 1 unit); a note
    // sits where its stored position says (note "a": 100, 100).
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      return (this.hasAttribute('data-sticky-id')
        ? { ...rect, left: 100, top: 100, x: 100, y: 100, width: 180, height: 140 }
        : rect) as DOMRect
    })
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('a mouse tap on the grip (under 4 px) opens the editor and sends nothing', async () => {
    const t = await setupWithDialog()
    t.fireEvent.pointerDown(t.grip, { pointerId: 1, pointerType: 'mouse', button: 0, clientX: 110, clientY: 105 })
    t.fireEvent.pointerMove(t.grip, { pointerId: 1, pointerType: 'mouse', buttons: 1, clientX: 112, clientY: 106 })
    t.fireEvent.pointerUp(t.grip, { pointerId: 1, pointerType: 'mouse', clientX: 112, clientY: 106 })
    t.fireEvent.click(t.grip, { detail: 1 })
    await t.waitFor(() => expect(t.container.querySelector('.sh-sticky-dialog')).not.toBeNull())
    expect(apiPatch).not.toHaveBeenCalled()
    expect((t.container.querySelector('[data-sticky-id="a"]') as HTMLElement)
      .style.getPropertyValue('--sh-sticky-x')).toBe('10%')
  })

  it('a touch tap on the grip opens the editor', async () => {
    const t = await setupWithDialog()
    t.fireEvent.pointerDown(t.grip, { pointerId: 2, pointerType: 'touch', button: 0, clientX: 110, clientY: 105 })
    t.fireEvent.pointerUp(t.grip, { pointerId: 2, pointerType: 'touch', clientX: 110, clientY: 105 })
    await t.waitFor(() => expect(t.container.querySelector('.sh-sticky-dialog')).not.toBeNull())
  })

  it('a touch long-press without movement neither opens nor moves', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const t = await setupWithDialog()
    t.fireEvent.pointerDown(t.grip, { pointerId: 3, pointerType: 'touch', button: 0, clientX: 110, clientY: 105 })
    await vi.advanceTimersByTimeAsync(700)
    t.fireEvent.pointerUp(t.grip, { pointerId: 3, pointerType: 'touch', clientX: 110, clientY: 105 })
    await vi.advanceTimersByTimeAsync(50)
    expect(t.container.querySelector('.sh-sticky-dialog')).toBeNull()
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('a real drag moves the note, sends one patch and does not open the editor', async () => {
    const t = await setupWithDialog()
    t.fireEvent.pointerDown(t.grip, { pointerId: 4, pointerType: 'mouse', button: 0, clientX: 110, clientY: 105 })
    t.fireEvent.pointerMove(t.grip, { pointerId: 4, pointerType: 'mouse', buttons: 1, clientX: 160, clientY: 135 })
    t.fireEvent.pointerMove(t.grip, { pointerId: 4, pointerType: 'mouse', buttons: 1, clientX: 210, clientY: 205 })
    t.fireEvent.pointerUp(t.grip, { pointerId: 4, pointerType: 'mouse', clientX: 210, clientY: 205 })
    t.fireEvent.click(t.grip, { detail: 1 })
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch).toHaveBeenCalledWith('/api/stickies/a', { position_x: 200, position_y: 200 })
    await new Promise(r => setTimeout(r, 20))
    expect(t.container.querySelector('.sh-sticky-dialog')).toBeNull()
  })

  it('Enter on the focused grip opens the editor', async () => {
    const t = await setupWithDialog()
    t.fireEvent.click(t.grip, { detail: 0 })
    await t.waitFor(() => expect(t.container.querySelector('.sh-sticky-dialog')).not.toBeNull())
  })

  const pe = (pointerId: number, x: number, y: number, extra: Record<string, unknown> = {}) =>
    ({ pointerId, pointerType: 'mouse', button: 0, buttons: 1, clientX: x, clientY: y, ...extra })
  const noteX = (t: { container: Element }) => (t.container.querySelector('[data-sticky-id="a"]') as HTMLElement)
    .style.getPropertyValue('--sh-sticky-x')

  it('pointercancel puts the note back and sends nothing', async () => {
    const t = await setupWithDialog()
    t.fireEvent.pointerDown(t.grip, pe(1, 110, 105))
    t.fireEvent.pointerMove(t.grip, pe(1, 210, 205))
    expect(noteX(t)).toBe('20%')
    expect(t.container.querySelector('.sh-sticky--dragging')).not.toBeNull()
    t.fireEvent.pointerCancel(t.grip, pe(1, 210, 205))
    expect(noteX(t)).toBe('10%')
    expect(t.container.querySelector('.sh-sticky--dragging')).toBeNull()
    expect(apiPatch).not.toHaveBeenCalled()
    expect(t.container.querySelector('.sh-sticky-dialog')).toBeNull()
  })

  it('unmounting mid-drag cleans up: reverted, nothing sent, no stuck drag', async () => {
    const t = await setupWithDialog()
    t.fireEvent.pointerDown(t.grip, pe(1, 110, 105))
    t.fireEvent.pointerMove(t.grip, pe(1, 210, 205))
    await resizeBoard(700) // the board turns into the grid: the note unmounts
    expect(t.container.querySelector('.sh-sticky-grid')).not.toBeNull()
    await resizeBoard(900)
    expect(noteX(t)).toBe('10%')
    expect(t.container.querySelector('.sh-sticky--dragging')).toBeNull()
    t.fireEvent.pointerMove(t.grip, pe(1, 400, 400))
    expect(apiPatch).not.toHaveBeenCalled()
    // A fresh drag still works.
    const grip = t.getByRole('button', { name: 'Move note: Note a' })
    t.fireEvent.pointerDown(grip, pe(2, 110, 105))
    t.fireEvent.pointerMove(grip, pe(2, 160, 105))
    t.fireEvent.pointerUp(grip, pe(2, 160, 105))
    expect(apiPatch).toHaveBeenCalledWith('/api/stickies/a', { position_x: 150, position_y: 100 })
  })

  it('a second pointer is ignored while a drag is active', async () => {
    const t = await setupWithDialog()
    t.fireEvent.pointerDown(t.grip, pe(1, 110, 105))
    t.fireEvent.pointerMove(t.grip, pe(1, 160, 105))
    t.fireEvent.pointerDown(t.grip, pe(2, 500, 500))
    t.fireEvent.pointerMove(t.grip, pe(2, 600, 600))
    t.fireEvent.pointerUp(t.grip, pe(2, 600, 600))
    expect(noteX(t)).toBe('15%')
    expect(apiPatch).not.toHaveBeenCalled()
    t.fireEvent.pointerUp(t.grip, pe(1, 210, 105))
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch).toHaveBeenCalledWith('/api/stickies/a', { position_x: 150, position_y: 100 })
  })

  it('a mouse move with no button held ends the drag (lost mouseup)', async () => {
    const t = await setupWithDialog()
    t.fireEvent.pointerDown(t.grip, pe(1, 110, 105))
    t.fireEvent.pointerMove(t.grip, pe(1, 160, 105))
    t.fireEvent.pointerMove(t.grip, pe(1, 300, 300, { buttons: 0 }))
    expect(t.container.querySelector('.sh-sticky--dragging')).toBeNull()
    expect(apiPatch).toHaveBeenCalledWith('/api/stickies/a', { position_x: 150, position_y: 100 })
    t.fireEvent.pointerMove(t.grip, pe(1, 400, 400))
    expect(noteX(t)).toBe('15%')
  })

  it('lostpointercapture ends the drag', async () => {
    const t = await setupWithDialog()
    t.fireEvent.pointerDown(t.grip, pe(1, 110, 105))
    t.fireEvent.pointerMove(t.grip, pe(1, 160, 105))
    t.fireEvent(t.grip, new Event('lostpointercapture'))
    expect(t.container.querySelector('.sh-sticky--dragging')).toBeNull()
    expect(apiPatch).toHaveBeenCalledTimes(1)
  })
})
