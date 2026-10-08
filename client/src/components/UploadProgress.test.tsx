/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/preact'
import {
  uploadWithProgress,
  uploadProgress,
  UploadProgressBar,
  type UploadEvent,
} from './UploadProgress'

// ── Fake XMLHttpRequest ────────────────────────────────────────────────
//
// We rebuild just enough of the XHR surface to drive
// ``uploadWithProgress`` deterministically — ``upload.onprogress``,
// ``upload.onload`` (body-fully-sent), ``onload`` (response landed),
// ``onerror``, plus the status / responseText accessors the success
// path reads.

class _FakeXHR {
  upload = {
    onprogress: null as ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null,
    onload: null as (() => void) | null,
  }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  status = 0
  responseText = ''
  _headers: Record<string, string> = {}
  open(_method: string, _url: string) { /* noop */ }
  setRequestHeader(k: string, v: string) { this._headers[k] = v }
  send(_body: FormData) { /* noop — tests drive the lifecycle by hand */ }

  // Test hooks: deliver phases to the consumer.
  _sendProgress(percent: number, total = 100) {
    this.upload.onprogress?.({ lengthComputable: true, loaded: percent, total })
  }
  _bodyFullySent() { this.upload.onload?.() }
  _respondOk(body: object) {
    this.status = 200
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }
  _respondFail(status: number) {
    this.status = status
    this.responseText = ''
    this.onload?.()
  }
  _networkError() { this.onerror?.() }
}

let lastXhr: _FakeXHR | null = null

beforeEach(() => {
  uploadProgress.value = null
  lastXhr = null
  ;(globalThis as any).XMLHttpRequest = function () {
    lastXhr = new _FakeXHR()
    return lastXhr
  } as any
})

afterEach(() => {
  delete (globalThis as any).XMLHttpRequest
})

describe('uploadWithProgress', () => {
  it('emits uploading events at every progress tick', async () => {
    const events: UploadEvent[] = []
    const file = new File(['x'], 'photo.jpg', { type: 'image/jpeg' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    // Simulate two progress ticks.
    lastXhr!._sendProgress(50)
    lastXhr!._sendProgress(99)
    lastXhr!._bodyFullySent()
    lastXhr!._respondOk({ url: 'api/media/x.webp', filename: 'x.webp' })
    await p

    const phases = events.map(e => e.phase)
    expect(phases).toEqual(['uploading', 'uploading', 'processing', 'done'])
    expect(events[0].percent).toBe(50)
    expect(events[1].percent).toBe(99)
  })

  it('emits a processing event when the body has fully landed', async () => {
    const events: UploadEvent[] = []
    const file = new File(['x'], 'clip.mp4', { type: 'video/mp4' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    lastXhr!._sendProgress(100)
    lastXhr!._bodyFullySent()
    // Server is now transcoding — no response yet. The chip should
    // show "Processing video…" in this window.
    expect(events[events.length - 1].phase).toBe('processing')
    expect(events[events.length - 1].percent).toBe(100)
    lastXhr!._respondOk({ url: 'api/media/x.webm', filename: 'x.webm' })
    await p
  })

  it('updates the legacy global signal during uploading + processing', async () => {
    const file = new File(['x'], 'photo.jpg', { type: 'image/jpeg' })
    const p = uploadWithProgress(file)
    lastXhr!._sendProgress(50)
    expect(uploadProgress.value?.phase).toBe('uploading')
    expect(uploadProgress.value?.percent).toBe(50)
    lastXhr!._bodyFullySent()
    expect(uploadProgress.value?.phase).toBe('processing')
    expect(uploadProgress.value?.percent).toBe(100)
    lastXhr!._respondOk({ url: 'api/media/x.webp', filename: 'x.webp' })
    await p
    // Cleared on terminal.
    expect(uploadProgress.value).toBeNull()
  })

  it('rejects + emits a failed event on HTTP failure', async () => {
    const events: UploadEvent[] = []
    const file = new File(['x'], 'big.bin', { type: 'application/octet-stream' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    lastXhr!._respondFail(413)
    await expect(p).rejects.toThrow(/413/)
    expect(events[events.length - 1].phase).toBe('failed')
  })

  it('resolves the local media url, falling back to it for the preview', async () => {
    const file = new File(['x'], 'photo.jpg', { type: 'image/jpeg' })
    const p = uploadWithProgress(file)
    lastXhr!._respondOk({ url: 'api/media/x.webp', filename: 'x.webp' })
    await expect(p).resolves.toEqual({
      url: 'api/media/x.webp',
      signed_url: 'api/media/x.webp',
      filename: 'x.webp',
    })
  })

  it('treats a 2xx without a url as a failed upload (never a bare filename)', async () => {
    // A bare filename is not a local media reference — the server would
    // refuse it (422 INVALID_MEDIA_URL) when the post is created, so
    // fail here, at the upload, where the user can retry.
    const events: UploadEvent[] = []
    const file = new File(['x'], 'photo.jpg', { type: 'image/jpeg' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    lastXhr!._respondOk({ filename: 'x.webp' })
    await expect(p).rejects.toThrow(/Upload failed/)
    expect(events[events.length - 1].phase).toBe('failed')
  })

  it('treats an unreadable 2xx body as a failed upload', async () => {
    const events: UploadEvent[] = []
    const file = new File(['x'], 'photo.jpg', { type: 'image/jpeg' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    lastXhr!.status = 201
    lastXhr!.responseText = 'not json'
    lastXhr!.onload?.()
    await expect(p).rejects.toThrow(/Upload failed/)
    expect(events[events.length - 1].phase).toBe('failed')
  })

  it('rejects + emits a failed event on network error', async () => {
    const events: UploadEvent[] = []
    const file = new File(['x'], 'big.bin', { type: 'application/octet-stream' })
    const p = uploadWithProgress(file, (e) => events.push(e))
    lastXhr!._networkError()
    await expect(p).rejects.toThrow(/Upload failed/)
    expect(events[events.length - 1].phase).toBe('failed')
  })
})

describe('UploadProgressBar', () => {
  afterEach(() => {
    uploadProgress.value = null
    cleanup()
  })

  it('renders nothing while idle', () => {
    uploadProgress.value = null
    const { container } = render(<UploadProgressBar />)
    expect(container.querySelector('.sh-upload-progress')).toBeNull()
  })

  it('shows the Uploading label + filename + percent + fill width', () => {
    uploadProgress.value = { filename: 'photo.jpg', percent: 42, phase: 'uploading' }
    const { container } = render(<UploadProgressBar />)
    const root = container.querySelector('.sh-upload-progress') as HTMLElement
    expect(root).not.toBeNull()
    expect(root.className).not.toContain('--processing')
    expect(container.querySelector('.sh-upload-progress__status')?.textContent)
      .toContain('Uploading')
    expect(container.querySelector('.sh-upload-progress__filename')?.textContent)
      .toBe('photo.jpg')
    expect(container.querySelector('.sh-upload-progress__pct')?.textContent)
      .toBe('42%')
    const fill = container.querySelector('.sh-upload-progress__fill') as HTMLElement
    expect(fill.style.width).toBe('42%')
    // ARIA: progressbar role + valuenow.
    expect(root.getAttribute('role')).toBe('progressbar')
    expect(root.getAttribute('aria-valuenow')).toBe('42')
    // Spinner only renders during the processing phase.
    expect(container.querySelector('.sh-upload-progress__spinner')).toBeNull()
  })

  it('shows the Processing label + spinner + holds at 100% while transcoding', () => {
    // §23.73 — once the body has fully streamed, the bar swaps the
    // percent for an animated stripe + spinner so the user can tell
    // "this isn't frozen, just slow" while the server transcodes.
    uploadProgress.value = { filename: 'clip.mp4', percent: 100, phase: 'processing' }
    const { container } = render(<UploadProgressBar />)
    const root = container.querySelector('.sh-upload-progress') as HTMLElement
    expect(root.className).toContain('sh-upload-progress--processing')
    expect(container.querySelector('.sh-upload-progress__status')?.textContent)
      .toContain('Processing')
    expect(container.querySelector('.sh-upload-progress__spinner')).not.toBeNull()
    // The numeric percent disappears once we're processing — the
    // barber-pole stripe + spinner are the indicator of liveness.
    expect(container.querySelector('.sh-upload-progress__pct')).toBeNull()
  })
})
