import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { useDragBuckets, composeDragHandlers } from './useDragBuckets'

function makeDataTransfer(): DataTransfer {
  const store = new Map<string, string>()
  const types: string[] = []
  return {
    types,
    effectAllowed: 'all',
    setData(type: string, val: string) {
      store.set(type, val)
      if (!types.includes(type)) types.push(type)
    },
    getData(type: string) { return store.get(type) ?? '' },
  } as unknown as DataTransfer
}

function Board({ onItemDrop, onColDrop, canDrop }: {
  onItemDrop: (id: string, b: string) => void
  onColDrop?: (id: string, b: string) => void
  canDrop?: (b: string) => boolean
}) {
  const items = useDragBuckets<string>({ mime: 'x/item', onDrop: onItemDrop, canDrop })
  const cols = useDragBuckets<string>({ mime: 'x/col', onDrop: onColDrop ?? (() => {}) })
  return (
    <div>
      {['a', 'b'].map(b => (
        <section key={b} data-testid={`bucket-${b}`}
                 data-over={String(items.overBucket === b)}
                 {...composeDragHandlers(items.bucketProps(b), cols.bucketProps(b))}>
          <header data-testid={`head-${b}`} {...cols.itemProps(b)}>{b}</header>
          <div data-testid={`row-${b}`} {...items.itemProps(`row-${b}`)}>row {b}</div>
        </section>
      ))}
      <span data-testid="dragging">{items.draggingId ?? 'none'}</span>
    </div>
  )
}

describe('useDragBuckets', () => {
  it('moves an item onto another bucket', () => {
    const onItemDrop = vi.fn()
    const { getByTestId } = render(<Board onItemDrop={onItemDrop} />)
    const dt = makeDataTransfer()
    fireEvent.dragStart(getByTestId('row-a'), { dataTransfer: dt })
    expect(getByTestId('dragging').textContent).toBe('row-a')
    fireEvent.dragOver(getByTestId('bucket-b'), { dataTransfer: dt })
    expect(getByTestId('bucket-b').dataset.over).toBe('true')
    fireEvent.drop(getByTestId('bucket-b'), { dataTransfer: dt })
    expect(onItemDrop).toHaveBeenCalledWith('row-a', 'b')
    expect(getByTestId('dragging').textContent).toBe('none')
    expect(getByTestId('bucket-b').dataset.over).toBe('false')
  })

  it('keeps two drag kinds apart on the same bucket', () => {
    const onItemDrop = vi.fn()
    const onColDrop = vi.fn()
    const { getByTestId } = render(<Board onItemDrop={onItemDrop} onColDrop={onColDrop} />)
    const dt = makeDataTransfer()
    fireEvent.dragStart(getByTestId('head-a'), { dataTransfer: dt })
    fireEvent.dragOver(getByTestId('bucket-b'), { dataTransfer: dt })
    fireEvent.drop(getByTestId('bucket-b'), { dataTransfer: dt })
    expect(onColDrop).toHaveBeenCalledWith('a', 'b')
    expect(onItemDrop).not.toHaveBeenCalled()
  })

  it('a refused bucket is not a drop target', () => {
    const onItemDrop = vi.fn()
    const { getByTestId } = render(
      <Board onItemDrop={onItemDrop} canDrop={(b) => b !== 'b'} />,
    )
    const dt = makeDataTransfer()
    fireEvent.dragStart(getByTestId('row-a'), { dataTransfer: dt })
    const over = fireEvent.dragOver(getByTestId('bucket-b'), { dataTransfer: dt })
    // fireEvent returns false when preventDefault was called.
    expect(over).toBe(true)
    fireEvent.drop(getByTestId('bucket-b'), { dataTransfer: dt })
    expect(onItemDrop).not.toHaveBeenCalled()
  })

  it('dragend clears the drag state', () => {
    const { getByTestId } = render(<Board onItemDrop={() => {}} />)
    const dt = makeDataTransfer()
    fireEvent.dragStart(getByTestId('row-a'), { dataTransfer: dt })
    fireEvent.dragOver(getByTestId('bucket-a'), { dataTransfer: dt })
    fireEvent.dragEnd(getByTestId('row-a'), { dataTransfer: dt })
    expect(getByTestId('dragging').textContent).toBe('none')
    expect(getByTestId('bucket-a').dataset.over).toBe('false')
  })

  it('leaving into a child keeps the highlight; leaving the bucket clears it', () => {
    const { getByTestId } = render(<Board onItemDrop={() => {}} />)
    const dt = makeDataTransfer()
    fireEvent.dragStart(getByTestId('row-a'), { dataTransfer: dt })
    fireEvent.dragOver(getByTestId('bucket-b'), { dataTransfer: dt })
    // A leave bubbling up from a child (pointer moved between children).
    fireEvent.dragLeave(getByTestId('row-b'), { dataTransfer: dt })
    expect(getByTestId('bucket-b').dataset.over).toBe('true')
    fireEvent.dragLeave(getByTestId('bucket-b'), { dataTransfer: dt })
    expect(getByTestId('bucket-b').dataset.over).toBe('false')
  })
})
