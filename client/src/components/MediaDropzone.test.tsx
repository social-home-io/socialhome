import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { MediaDropzone } from './MediaDropzone'
import * as Toast from './Toast'

function makeFile(name = 'pic.jpg', type = 'image/jpeg') {
  return new File([new Uint8Array([1, 2, 3])], name, { type })
}

describe('MediaDropzone', () => {
  it('routes picked files through onFiles', async () => {
    const onFiles = vi.fn().mockResolvedValue(undefined)
    const { container } = render(
      <MediaDropzone onFiles={onFiles} hint="Drop here" pickLabel="pick…" />,
    )
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(input, 'files', {
      value: [makeFile('a.jpg'), makeFile('b.jpg')],
      configurable: true,
    })
    fireEvent.change(input)
    // onFiles is async — flush a microtask before asserting.
    await Promise.resolve()
    expect(onFiles).toHaveBeenCalledTimes(1)
    expect(onFiles.mock.calls[0][0]).toHaveLength(2)
  })

  it('toggles the dragging class on dragover / dragleave', () => {
    const { container } = render(
      <MediaDropzone onFiles={vi.fn()} hint="Drop" pickLabel="pick" draggingHint="Release" />,
    )
    const drop = container.querySelector('.sh-mediadrop') as HTMLElement
    expect(drop.classList.contains('sh-mediadrop--dragging')).toBe(false)
    fireEvent.dragOver(drop)
    expect(drop.classList.contains('sh-mediadrop--dragging')).toBe(true)
    expect(drop.textContent).toContain('Release')
    fireEvent.dragLeave(drop)
    expect(drop.classList.contains('sh-mediadrop--dragging')).toBe(false)
  })

  it('toasts when the picker callback fires with an empty FileList', async () => {
    // Reproduces the HA Android Companion App symptom: the chooser
    // returns to the page but ``input.files`` is empty (the
    // WebChromeClient didn't propagate the URI). Before the toast,
    // the composer was completely silent and looked broken.
    const toastSpy = vi.spyOn(Toast, 'showToast').mockImplementation(() => 0)
    const onFiles = vi.fn()
    const { container } = render(
      <MediaDropzone onFiles={onFiles} hint="x" pickLabel="pick" />,
    )
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [], configurable: true })
    fireEvent.change(input)
    await Promise.resolve()
    expect(onFiles).not.toHaveBeenCalled()
    expect(toastSpy).toHaveBeenCalledTimes(1)
    expect(toastSpy.mock.calls[0][1]).toBe('error')
    toastSpy.mockRestore()
  })

  it('wraps the picker affordance in a label so the input opens via native gesture', () => {
    // The label-wrap pattern is what gets a single user-gesture
    // flowing into the file input's chooser without the synthetic
    // ``ref.current.click()`` indirection that historically tripped
    // the HA Android Companion App's WebView.
    const { container } = render(
      <MediaDropzone onFiles={vi.fn()} hint="x" pickLabel="pick…" />,
    )
    const label = container.querySelector('label.sh-link')
    expect(label).not.toBeNull()
    expect(label?.querySelector('input[type="file"]')).not.toBeNull()
  })

  it('skips onFiles and disables the picker when disabled', async () => {
    const onFiles = vi.fn()
    const { container } = render(
      <MediaDropzone onFiles={onFiles} disabled hint="x" pickLabel="pick" />,
    )
    const drop = container.querySelector('.sh-mediadrop') as HTMLElement
    expect(drop.classList.contains('sh-mediadrop--disabled')).toBe(true)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    expect(input.disabled).toBe(true)
    // Drag-over should not trigger the dragging state when disabled —
    // the user gets a clear visual signal that the surface is inert.
    fireEvent.dragOver(drop)
    expect(drop.classList.contains('sh-mediadrop--dragging')).toBe(false)
    expect(onFiles).not.toHaveBeenCalled()
  })
})
