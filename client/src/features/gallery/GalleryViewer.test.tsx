import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup, waitFor } from '@testing-library/preact'
import { GalleryViewer, type GalleryViewerProps, type ViewerItem } from './GalleryViewer'

function item(n: number, extra: Partial<ViewerItem> = {}): ViewerItem {
  return {
    id: `it-${n}`, item_type: 'photo', url: `api/media/p${n}.webp`,
    thumbnail_url: `api/media/t${n}.webp`, width: 2560, height: 1920,
    uploaded_by: 'u-anna', created_at: '2026-07-14T10:00:00+00:00',
    taken_at: '2026-07-12', ...extra,
  }
}

function viewer(over: Partial<GalleryViewerProps> = {}) {
  const props: GalleryViewerProps = {
    items: [item(1), item(2), item(3)],
    index: 1,
    total: 84,
    albumName: 'Summer 2026',
    inSpace: false,
    uploaderName: () => 'Anna',
    canDelete: () => false,
    reportFor: () => null,
    onGo: vi.fn(),
    onClose: vi.fn(),
    onDelete: vi.fn(async () => undefined),
    ...over,
  }
  return { props, view: render(<GalleryViewer {...props} />) }
}

afterEach(() => { cleanup() })

describe('GalleryViewer', () => {
  it('is a named dialog with the position, the day taken and the album', () => {
    const { view } = viewer()
    expect(view.getByRole('dialog', { name: 'Photo 2 of 84' })).toBeTruthy()
    expect(view.getByText('2 of 84 · Summer 2026')).toBeTruthy()
    expect(view.getByText(/July 12, 2026/)).toBeTruthy()
    // Focus lands inside, on Close.
    expect(document.activeElement).toBe(view.getByRole('button', { name: /Close viewer/ }))
  })

  it('arrow keys on the viewer step through the album; Esc closes', () => {
    const { props, view } = viewer()
    const dialog = view.getByRole('dialog')
    fireEvent.keyDown(dialog, { key: 'ArrowRight' })
    expect(props.onGo).toHaveBeenLastCalledWith(2)
    fireEvent.keyDown(dialog, { key: 'ArrowLeft' })
    expect(props.onGo).toHaveBeenLastCalledWith(0)
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(props.onClose).toHaveBeenCalled()
  })

  it('arrow keys are not taken from the window — the page and a video keep theirs', () => {
    const { props, view } = viewer({ items: [item(1), item(2, { item_type: 'video', url: 'api/media/v.webm' }), item(3)] })
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(props.onGo).not.toHaveBeenCalled()
    // A focused video's scrubber gets the arrows, not the viewer.
    const video = view.container.querySelector('video')!
    fireEvent.keyDown(video, { key: 'ArrowRight' })
    expect(props.onGo).not.toHaveBeenCalled()
  })

  it('a video plays inline with its poster, never autoplaying', () => {
    const { view } = viewer({ items: [item(1, { item_type: 'video', url: 'api/media/v.webm' })], index: 0 })
    const video = view.container.querySelector('video')!
    expect(video.getAttribute('poster')).toBe('api/media/t1.webp')
    expect(video.hasAttribute('playsinline')).toBe(true)
    expect(video.hasAttribute('autoplay')).toBe(false)
  })

  it('a swipe left goes to the next photo, right to the previous', () => {
    const { props, view } = viewer()
    const stage = view.container.querySelector('.sh-gviewer-stage')!
    fireEvent.pointerDown(stage, { pointerId: 1, pointerType: 'touch', clientX: 300, clientY: 400 })
    fireEvent.pointerMove(stage, { pointerId: 1, pointerType: 'touch', clientX: 150, clientY: 410 })
    fireEvent.pointerUp(stage, { pointerId: 1, pointerType: 'touch', clientX: 150, clientY: 410 })
    expect(props.onGo).toHaveBeenLastCalledWith(2)
    fireEvent.pointerDown(stage, { pointerId: 2, pointerType: 'touch', clientX: 100, clientY: 400 })
    fireEvent.pointerMove(stage, { pointerId: 2, pointerType: 'touch', clientX: 260, clientY: 400 })
    fireEvent.pointerUp(stage, { pointerId: 2, pointerType: 'touch', clientX: 260, clientY: 400 })
    expect(props.onGo).toHaveBeenLastCalledWith(0)
  })

  it('+ zooms, 0 fits again; zoomed, the arrows pan instead of leaving the photo', () => {
    const { props, view } = viewer()
    const dialog = view.getByRole('dialog')
    const photo = () => view.container.querySelector('.sh-gviewer-photo') as HTMLImageElement
    fireEvent.keyDown(dialog, { key: '+' })
    expect(dialog.className).toContain('sh-gviewer--zoomed')
    fireEvent.keyDown(dialog, { key: 'ArrowRight' })
    expect(props.onGo).not.toHaveBeenCalled()
    fireEvent.keyDown(dialog, { key: '0' })
    expect(dialog.className).not.toContain('sh-gviewer--zoomed')
    expect(photo()).toBeTruthy()
  })

  it('double-click zooms in and out', () => {
    const { view } = viewer()
    const dialog = view.getByRole('dialog')
    const stage = view.container.querySelector('.sh-gviewer-stage')!
    fireEvent.dblClick(stage, { clientX: 10, clientY: 10 })
    expect(dialog.className).toContain('sh-gviewer--zoomed')
    fireEvent.dblClick(stage, { clientX: 10, clientY: 10 })
    expect(dialog.className).not.toContain('sh-gviewer--zoomed')
  })

  it('a touch double-tap zooms — and the browser\'s dblclick after it does not zoom back out', () => {
    const { view } = viewer()
    const dialog = view.getByRole('dialog')
    const stage = view.container.querySelector('.sh-gviewer-stage')!
    const tap = (id: number, t: number) => {
      fireEvent.pointerDown(stage, { pointerId: id, pointerType: 'touch', clientX: 50, clientY: 50, timeStamp: t })
      fireEvent.pointerUp(stage, { pointerId: id, pointerType: 'touch', clientX: 50, clientY: 50, timeStamp: t + 5 })
    }
    tap(1, 1000)
    tap(2, 1100)
    expect(dialog.className).toContain('sh-gviewer--zoomed')
    fireEvent.dblClick(stage, { clientX: 50, clientY: 50 })
    expect(dialog.className).toContain('sh-gviewer--zoomed')
  })

  it('Tab stays inside the viewer', () => {
    const { view } = viewer()
    const dialog = view.getByRole('dialog')
    const buttons = [...dialog.querySelectorAll<HTMLElement>('button:not([disabled]), a[href]')]
    buttons.at(-1)!.focus()
    fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
    expect(document.activeElement).toBe(buttons[0])
    fireEvent.keyDown(document.activeElement!, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(buttons.at(-1))
  })

  it('Info: date taken, who added it, the album, the size — caption read-only', () => {
    const { view } = viewer({ items: [item(1, { caption: 'Oma am Bodensee' })], index: 0, total: 1 })
    fireEvent.click(view.getByRole('button', { name: 'Info' }))
    const info = view.getByRole('complementary')
    expect(info.textContent).toContain('Oma am Bodensee')
    expect(info.textContent).toContain('by Anna · Jul 14, 2026')
    expect(info.textContent).toContain('Summer 2026')
    expect(info.textContent).toContain('2560 × 1920')
    expect(info.querySelector('input, textarea')).toBeNull()
    // Esc closes Info first, then the viewer.
    fireEvent.keyDown(view.getByRole('dialog'), { key: 'Escape' })
    expect(view.queryByRole('complementary')).toBeNull()
  })

  it('offers Delete only when allowed, and asks first', async () => {
    let r = viewer()
    expect(r.view.queryByRole('button', { name: 'Delete' })).toBeNull()
    cleanup()
    r = viewer({ canDelete: () => true })
    fireEvent.click(r.view.getByRole('button', { name: 'Delete' }))
    const ask = r.view.getByRole('alertdialog', { name: 'Delete this photo?' })
    expect(ask.textContent).toContain('everyone in your household')
    // The safe answer has the focus.
    expect(document.activeElement?.textContent).toBe('Cancel')
    fireEvent.click(r.view.getAllByRole('button', { name: 'Delete' }).at(-1)!)
    await waitFor(() => expect(r.props.onDelete).toHaveBeenCalledWith(r.props.items[1]))
    await waitFor(() => expect(r.view.queryByRole('alertdialog')).toBeNull())
  })

  it('a failed delete says so inside the viewer (toasts sit behind it)', async () => {
    const { view } = viewer({ canDelete: () => true, onDelete: vi.fn(async () => { throw new Error('offline') }) })
    fireEvent.click(view.getByRole('button', { name: 'Delete' }))
    fireEvent.click(view.getAllByRole('button', { name: 'Delete' }).at(-1)!)
    expect(await view.findByText("Couldn't delete it. Check your connection and try again.")).toBeTruthy()
  })

  it('Report closes the viewer and opens the report', () => {
    const report = vi.fn()
    const { props, view } = viewer({ reportFor: () => report })
    fireEvent.click(view.getByRole('button', { name: 'Report' }))
    expect(props.onClose).toHaveBeenCalled()
    expect(report).toHaveBeenCalled()
  })

  it('while a link is looked up it shows a skeleton and can be closed', () => {
    const { props, view } = viewer({ items: [], index: -1, total: 0 })
    expect(view.getAllByRole('status', { name: 'Loading...' }).length).toBeGreaterThan(0)
    fireEvent.click(view.getByRole('button', { name: /Close viewer/ }))
    expect(props.onClose).toHaveBeenCalled()
  })

  it('a video still processing says so instead of a broken player', () => {
    const { view } = viewer({ items: [item(1, { item_type: 'video', processing: true })], index: 0, total: 1 })
    expect(view.container.querySelector('video')).toBeNull()
    expect(view.getByText(/still being processed/)).toBeTruthy()
    expect(view.queryByText('Download')).toBeNull()
  })
})
