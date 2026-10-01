import { describe, it, expect, vi, afterEach, onTestFinished } from 'vitest'
import { render, fireEvent, act } from '@testing-library/preact'
import {
  edgeScrollDelta, insertionIndex, passedThreshold, useBoardDrag, LONG_PRESS_MS, CLICK_GUARD_MS,
  type DropTarget,
} from './useBoardDrag'

describe('drag geometry', () => {
  it('passedThreshold: more than 4 px (mouse) / the given slop', () => {
    expect(passedThreshold(3, 2)).toBe(false)
    expect(passedThreshold(4, 0)).toBe(false)
    expect(passedThreshold(3, 3)).toBe(true)
    expect(passedThreshold(6, 0, 8)).toBe(false)
  })

  it('insertionIndex counts the card midpoints above the pointer', () => {
    expect(insertionIndex([10, 50, 90], 0)).toBe(0)
    expect(insertionIndex([10, 50, 90], 60)).toBe(2)
    expect(insertionIndex([10, 50, 90], 200)).toBe(3)
    expect(insertionIndex([], 5)).toBe(0)
  })

  it('edgeScrollDelta scrolls near the edges, faster closer, not in the middle', () => {
    expect(edgeScrollDelta(400, 0, 800)).toBe(0)
    expect(edgeScrollDelta(0, 0, 800)).toBe(-18)
    expect(edgeScrollDelta(28, 0, 800)).toBe(-9)
    expect(edgeScrollDelta(800, 0, 800)).toBe(18)
    expect(edgeScrollDelta(10, 0, 100)).toBe(0) // too small to scroll
  })
})

// ─── The hook against a tiny board ──────────────────────────────────

function Harness({ onDrop, canDrag = () => true }: {
  onDrop: (id: string, t: DropTarget) => void
  canDrag?: (id: string) => boolean
}) {
  const d = useBoardDrag({ canDrag, onDrop })
  return (
    <div>
      <section data-board-drop="todo" data-testid="todo">
        {['a', 'b'].map(id => (
          <article key={id} data-board-card data-task-id={id} data-testid={id} {...d.cardProps(id)}>
            {id}
            <button data-no-drag>menu</button>
          </article>
        ))}
      </section>
      <section data-board-drop="done" data-testid="done" />
      <button data-board-drop="in_progress" data-board-drop-kind="chip" data-testid="chip">chip</button>
      <output data-testid="state">
        {d.drag ? `drag:${d.drag.id}` : 'idle'}|{d.over ? `${d.over.status}@${d.over.index}` : '-'}|{d.pressingId ?? ''}
      </output>
    </div>
  )
}

function stubHit(map: (x: number, y: number) => Element | null) {
  const original = document.elementFromPoint
  document.elementFromPoint = map as typeof document.elementFromPoint
  onTestFinished(() => { document.elementFromPoint = original })
}

function rect(el: Element, top: number, height = 40) {
  ;(el as HTMLElement).getBoundingClientRect = () =>
    ({ top, height, left: 0, width: 200, right: 200, bottom: top + height, x: 0, y: top, toJSON() {} }) as DOMRect
}

function setup(canDrag?: (id: string) => boolean) {
  const onDrop = vi.fn()
  const r = render(<Harness onDrop={onDrop} canDrag={canDrag} />)
  rect(r.getByTestId('a'), 0)
  rect(r.getByTestId('b'), 50)
  const state = () => r.getByTestId('state').textContent
  return { ...r, onDrop, state }
}

const pd = (el: Element, x: number, y: number, pointerType = 'mouse') =>
  fireEvent.pointerDown(el, { pointerId: 1, isPrimary: true, button: 0, clientX: x, clientY: y, pointerType })
const pm = (x: number, y: number) =>
  fireEvent.pointerMove(window, { pointerId: 1, clientX: x, clientY: y })
const pu = (x: number, y: number) =>
  fireEvent.pointerUp(window, { pointerId: 1, clientX: x, clientY: y })

afterEach(() => { vi.useRealTimers() })

describe('useBoardDrag', () => {
  it('a mouse drag starts after 4 px and drops at the insertion index', () => {
    const t = setup()
    stubHit((x) => (x > 500 ? t.getByTestId('done') : t.getByTestId('todo')))
    pd(t.getByTestId('a'), 10, 10)
    pm(12, 11)
    expect(t.state()).toBe('idle|-|')
    pm(20, 80)
    // Over the to-do column, below b's midpoint (70) → index 1 (after b).
    expect(t.state()).toBe('drag:a|todo@1|')
    pm(600, 10)
    expect(t.state()).toBe('drag:a|done@0|')
    pu(600, 10)
    expect(t.onDrop).toHaveBeenCalledWith('a', { status: 'done', index: 0, kind: 'column' })
    expect(t.state()).toBe('idle|-|')
  })

  it('a click without movement neither drags nor drops', () => {
    const t = setup()
    stubHit(() => t.getByTestId('todo'))
    pd(t.getByTestId('a'), 10, 10)
    pu(10, 10)
    expect(t.onDrop).not.toHaveBeenCalled()
  })

  it('the click that ends a drag is swallowed', () => {
    const t = setup()
    stubHit(() => t.getByTestId('done'))
    const onClick = vi.fn()
    t.getByTestId('a').addEventListener('click', onClick)
    pd(t.getByTestId('a'), 10, 10)
    pm(40, 40)
    pu(40, 40)
    fireEvent.click(t.getByTestId('a'))
    expect(onClick).not.toHaveBeenCalled()
  })

  it('a click that arrives a task later is still swallowed, but only that one', () => {
    vi.useFakeTimers()
    const t = setup()
    stubHit(() => t.getByTestId('done'))
    const onClick = vi.fn()
    t.getByTestId('a').addEventListener('click', onClick)
    pd(t.getByTestId('a'), 10, 10)
    pm(40, 40)
    pu(40, 40)
    act(() => { vi.advanceTimersByTime(20) })
    fireEvent.click(t.getByTestId('a'))
    expect(onClick).not.toHaveBeenCalled()
    fireEvent.click(t.getByTestId('a'))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('the guard lapses: a much later click goes through', () => {
    vi.useFakeTimers()
    const t = setup()
    stubHit(() => t.getByTestId('done'))
    const onClick = vi.fn()
    t.getByTestId('a').addEventListener('click', onClick)
    pd(t.getByTestId('a'), 10, 10)
    pm(40, 40)
    pu(40, 40)
    act(() => { vi.advanceTimersByTime(CLICK_GUARD_MS + 1) })
    fireEvent.click(t.getByTestId('a'))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('Escape cancels a drag: nothing drops', () => {
    const t = setup()
    stubHit(() => t.getByTestId('done'))
    pd(t.getByTestId('a'), 10, 10)
    pm(40, 40)
    expect(t.state()).toMatch(/^drag:a/)
    fireEvent.keyDown(window, { key: 'Escape' })
    pu(40, 40)
    expect(t.onDrop).not.toHaveBeenCalled()
    expect(t.state()).toBe('idle|-|')
  })

  it('a release outside any drop zone drops nothing', () => {
    const t = setup()
    stubHit(() => null)
    pd(t.getByTestId('a'), 10, 10)
    pm(40, 40)
    pu(40, 40)
    expect(t.onDrop).not.toHaveBeenCalled()
  })

  it('a column chip is a drop target for the bottom of its column', () => {
    const t = setup()
    stubHit(() => t.getByTestId('chip'))
    pd(t.getByTestId('b'), 10, 60)
    pm(30, 90)
    pu(30, 90)
    expect(t.onDrop).toHaveBeenCalledWith('b', { status: 'in_progress', index: Infinity, kind: 'chip' })
  })

  it('touch: a long-press starts the drag; the finger then drags', () => {
    vi.useFakeTimers()
    const t = setup()
    stubHit(() => t.getByTestId('done'))
    pd(t.getByTestId('a'), 10, 10, 'touch')
    expect(t.state()).toBe('idle|-|a')
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS) })
    expect(t.state()).toBe('drag:a|done@0|')
    // While dragging the page must not scroll under the finger.
    const move = new TouchEvent('touchmove', { cancelable: true, bubbles: true })
    t.getByTestId('a').dispatchEvent(move)
    expect(move.defaultPrevented).toBe(true)
    pm(30, 30)
    pu(30, 30)
    expect(t.onDrop).toHaveBeenCalledWith('a', { status: 'done', index: 0, kind: 'column' })
  })

  it('touch: the click a long-press release may send is swallowed too', () => {
    vi.useFakeTimers()
    const t = setup()
    stubHit(() => t.getByTestId('done'))
    const onClick = vi.fn()
    t.getByTestId('a').addEventListener('click', onClick)
    pd(t.getByTestId('a'), 10, 10, 'touch')
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS) })
    pu(10, 10)
    fireEvent.click(t.getByTestId('a'))
    expect(onClick).not.toHaveBeenCalled()
  })

  it('touch: moving before the long-press is a scroll — no drag', () => {
    vi.useFakeTimers()
    const t = setup()
    stubHit(() => t.getByTestId('done'))
    pd(t.getByTestId('a'), 10, 10, 'touch')
    pm(10, 30)
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS * 2) })
    expect(t.state()).toBe('idle|-|')
    const move = new TouchEvent('touchmove', { cancelable: true, bubbles: true })
    t.getByTestId('a').dispatchEvent(move)
    expect(move.defaultPrevented).toBe(false)
    pu(10, 30)
    expect(t.onDrop).not.toHaveBeenCalled()
  })

  it('a card that may not be changed never drags', () => {
    const t = setup(id => id !== 'a')
    stubHit(() => t.getByTestId('done'))
    pd(t.getByTestId('a'), 10, 10)
    pm(60, 60)
    pu(60, 60)
    expect(t.onDrop).not.toHaveBeenCalled()
  })

  it('pressing a [data-no-drag] control does not start a drag', () => {
    const t = setup()
    stubHit(() => t.getByTestId('done'))
    pd(t.getByTestId('a').querySelector('button')!, 10, 10)
    pm(60, 60)
    expect(t.state()).toBe('idle|-|')
  })
})
