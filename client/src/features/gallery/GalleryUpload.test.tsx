import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, cleanup, waitFor, act } from '@testing-library/preact'

const addFiles = vi.fn((_files: File[], _into: unknown) => true)
vi.mock('./uploadQueue', () => ({ addFiles: (f: File[], i: unknown) => addFiles(f, i) }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))

import { UploadButtons, useUploadControls } from './GalleryUpload'
import type { UploadTarget } from './uploadQueue'

const into: UploadTarget = { spaceId: null, albumId: 'al-1', name: 'Holiday', isDefault: false }

function Harness({ enabled = true, target = into }: { enabled?: boolean; target?: UploadTarget | null }) {
  const c = useUploadControls(target, enabled)
  return (
    <div>
      {enabled && <UploadButtons controls={c} />}
      {c.elements}
      <span data-testid="cam">{String(c.fromCamera)}</span>
    </div>
  )
}

function dropEvent(type: string, files: File[]) {
  const ev = new Event(type, { bubbles: true, cancelable: true }) as DragEvent
  Object.defineProperty(ev, 'dataTransfer', {
    value: { types: ['Files'], files, items: [], dropEffect: 'none' },
  })
  return ev
}

beforeEach(() => { addFiles.mockClear(); addFiles.mockImplementation(() => true); showToast.mockReset() })
afterEach(() => cleanup())

describe('useUploadControls', () => {
  it('the picker is multi-select with the accepted types; the camera input captures', () => {
    const view = render(<Harness />)
    const pick = view.container.querySelector('[data-testid=gallery-upload-input]') as HTMLInputElement
    const cam = view.container.querySelector('[data-testid=gallery-camera-input]') as HTMLInputElement
    expect(pick.multiple).toBe(true)
    expect(pick.accept).toContain('image/heic')
    expect(cam.getAttribute('capture')).toBe('environment')
    expect(view.getByRole('button', { name: 'Take a photo or video' })).toBeTruthy()
  })

  it('the camera button keeps its name when a narrow phone hides the word', () => {
    // CSS hides ``__label`` on a narrow phone; the button still has an
    // accessible name and a tooltip, and the 📷 stays visible.
    const view = render(<Harness />)
    const cam = view.getByRole('button', { name: 'Take a photo or video' })
    expect(cam.getAttribute('title')).toBe('Take a photo or video')
    expect(cam.querySelector('.sh-upload-camera-btn__label')?.textContent?.trim()).toBe('Camera')
  })

  it('picked files go to the queue for the page target', () => {
    const view = render(<Harness />)
    const pick = view.container.querySelector('[data-testid=gallery-upload-input]') as HTMLInputElement
    const f = new File(['x'], 'a.jpg', { type: 'image/jpeg' })
    Object.defineProperty(pick, 'files', { value: [f], configurable: true })
    fireEvent.change(pick)
    expect(addFiles).toHaveBeenCalledWith([f], into)
    expect(view.getByTestId('cam').textContent).toBe('false')
  })

  it('a picker that returns nothing says so instead of staying silent', () => {
    const view = render(<Harness />)
    const pick = view.container.querySelector('[data-testid=gallery-upload-input]') as HTMLInputElement
    Object.defineProperty(pick, 'files', { value: [], configurable: true })
    fireEvent.change(pick)
    expect(addFiles).not.toHaveBeenCalled()
    expect(showToast).toHaveBeenCalledWith(
      "The file picker didn't return anything. Try again, or drag the file in.", 'error',
    )
  })

  it('the picker picks one photo at a time in the HA Android app before 2026.11.1', () => {
    const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (Linux; Android 16; SM-S931B) Chrome/141.0 Mobile Safari/537.36 Home Assistant/2026.10.1-full (Android 16; SM-S931B)',
    )
    try {
      const view = render(<Harness />)
      const pick = view.container.querySelector('[data-testid=gallery-upload-input]') as HTMLInputElement
      expect(pick.multiple).toBe(false)
    } finally {
      ua.mockRestore()
    }
  })

  it('a camera shot is remembered ("Take another")', () => {
    const view = render(<Harness />)
    const cam = view.container.querySelector('[data-testid=gallery-camera-input]') as HTMLInputElement
    Object.defineProperty(cam, 'files', { value: [new File(['x'], 'shot.jpg', { type: 'image/jpeg' })], configurable: true })
    fireEvent.change(cam)
    expect(view.getByTestId('cam').textContent).toBe('true')
  })

  it('drop anywhere: an overlay names the album, the drop adds the files', async () => {
    const view = render(<Harness />)
    const f = new File(['x'], 'a.jpg', { type: 'image/jpeg' })
    act(() => { window.dispatchEvent(dropEvent('dragenter', [f])) })
    expect(view.getByText('Drop to upload into “Holiday”')).toBeTruthy()
    const drop = dropEvent('drop', [f])
    act(() => { window.dispatchEvent(drop) })
    expect(drop.defaultPrevented).toBe(true)
    await waitFor(() => expect(addFiles).toHaveBeenCalledWith([f], into))
    expect(view.queryByText('Drop to upload into “Holiday”')).toBeNull()
  })

  it('the default album target says "drop to upload"', () => {
    const view = render(<Harness target={{ spaceId: null, albumId: null, name: 'Photos', isDefault: true }} />)
    act(() => { window.dispatchEvent(dropEvent('dragenter', [])) })
    expect(view.getByText('Drop photos and videos to upload')).toBeTruthy()
  })

  it('disabled: no inputs, and a drop is left to the browser', () => {
    const view = render(<Harness enabled={false} />)
    expect(view.container.querySelector('input')).toBeNull()
    const drop = dropEvent('drop', [new File(['x'], 'a.jpg')])
    window.dispatchEvent(drop)
    expect(drop.defaultPrevented).toBe(false)
    expect(addFiles).not.toHaveBeenCalled()
  })

  it('a queue busy for another gallery says so', () => {
    addFiles.mockImplementation(() => false)
    const view = render(<Harness />)
    const pick = view.container.querySelector('[data-testid=gallery-upload-input]') as HTMLInputElement
    Object.defineProperty(pick, 'files', { value: [new File(['x'], 'a.jpg')], configurable: true })
    fireEvent.change(pick)
    expect(showToast).toHaveBeenCalledWith(
      "Another gallery is still uploading. Add these when it's done.", 'info',
    )
  })
})
