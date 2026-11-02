import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  ACCEPT, HOUSEHOLD_VIDEO_MAX_BYTES, MAX_UPLOAD_BYTES, MAX_DROP_FILES, canPreview,
  checkFile, dragHasFiles, filesFromDrop, formatDuration, formatSize, maxBytes,
  readVideoDuration, videoTooLong, withType,
} from './uploadFiles'

function file(name: string, type: string, size = 10): File {
  const f = new File(['x'], name, { type })
  Object.defineProperty(f, 'size', { value: size })
  return f
}

describe('checkFile', () => {
  it('takes the photo and video types the server converts', () => {
    expect(checkFile(file('a.jpg', 'image/jpeg'))).toEqual({ kind: 'photo', mime: 'image/jpeg', problem: null })
    expect(checkFile(file('a.png', 'image/png')).problem).toBeNull()
    expect(checkFile(file('a.mov', 'video/quicktime'))).toEqual({ kind: 'video', mime: 'video/quicktime', problem: null })
  })

  it('names the type from the extension when the browser gave none', () => {
    expect(checkFile(file('IMG_1.HEIC', ''))).toEqual({ kind: 'photo', mime: 'image/heic', problem: null })
    expect(checkFile(file('clip.mp4', 'application/octet-stream')).mime).toBe('video/mp4')
    expect(checkFile(file('x.heif', 'image/heif')).mime).toBe('image/heic')
  })

  it('refuses what the server would refuse — before sending a byte', () => {
    expect(checkFile(file('a.pdf', 'application/pdf')).problem).toBe('unsupported')
    expect(checkFile(file('scan.tiff', 'image/tiff')).problem).toBe('unsupported')
    expect(checkFile(file('noext', '')).problem).toBe('unsupported')
    expect(checkFile(file('a.jpg', 'image/jpeg', 0)).problem).toBe('empty')
    expect(checkFile(file('a.jpg', 'image/jpeg', MAX_UPLOAD_BYTES + 1)).problem).toBe('too_large')
    expect(checkFile(file('a.jpg', 'image/jpeg', MAX_UPLOAD_BYTES)).problem).toBeNull()
  })

  it('the picker accepts the same list plus .heic', () => {
    expect(ACCEPT).toContain('image/heic')
    expect(ACCEPT).toContain('.heic')
    expect(ACCEPT).toContain('video/quicktime')
  })
})

describe('withType', () => {
  it('re-types a type-less file and keeps a typed one as it is', () => {
    const f = file('IMG.HEIC', '')
    const typed = withType(f, 'image/heic')
    expect(typed.type).toBe('image/heic')
    expect(typed.name).toBe('IMG.HEIC')
    const jpg = file('a.jpg', 'image/jpeg')
    expect(withType(jpg, 'image/jpeg')).toBe(jpg)
  })
})

describe('canPreview', () => {
  const ua = navigator.userAgent
  afterEach(() => { Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true }) })

  it('never tries to draw a HEIC outside Safari, nor a video', async () => {
    Object.defineProperty(navigator, 'userAgent', {
      value: 'Mozilla/5.0 (X11; Linux) AppleWebKit/537.36 Chrome/140.0 Safari/537.36', configurable: true,
    })
    vi.resetModules()
    const m = await import('./uploadFiles')
    const heic = file('a.heic', 'image/heic')
    expect(m.canPreview(heic, m.checkFile(heic))).toBe(false)
    const jpg = file('a.jpg', 'image/jpeg')
    expect(m.canPreview(jpg, m.checkFile(jpg))).toBe(true)
    const mp4 = file('a.mp4', 'video/mp4')
    expect(m.canPreview(mp4, m.checkFile(mp4))).toBe(false)
  })

  it('Safari draws HEIC itself', async () => {
    Object.defineProperty(navigator, 'userAgent', {
      value: 'Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 Version/19.0 Mobile/15E148 Safari/604.1', configurable: true,
    })
    vi.resetModules()
    const m = await import('./uploadFiles')
    const heic = file('a.heic', 'image/heic')
    expect(m.canPreview(heic, m.checkFile(heic))).toBe(true)
  })

  it('an unsupported file has no preview', () => {
    const f = file('a.pdf', 'application/pdf')
    expect(canPreview(f, checkFile(f))).toBe(false)
  })
})

describe('drops', () => {
  type E = {
    isFile: boolean; isDirectory: boolean; name: string
    file?: (ok: (f: File) => void) => void
    createReader?: () => { readEntries: (ok: (es: E[]) => void) => void }
  }
  const fileEntry = (f: File): E => ({ isFile: true, isDirectory: false, name: f.name, file: ok => ok(f) })
  const dirEntry = (name: string, children: E[]): E => ({
    isFile: false, isDirectory: true, name,
    createReader: () => {
      // Hands out the folder in chunks of two, then an empty chunk.
      let i = 0
      return { readEntries: ok => { ok(children.slice(i, i + 2)); i += 2 } }
    },
  })
  const dt = (entries: E[], files: File[] = []) => ({
    types: ['Files'],
    files,
    items: entries.map(e => ({ kind: 'file', webkitGetAsEntry: () => e })),
  }) as unknown as DataTransfer

  it('walks into dropped folders (and skips hidden files)', async () => {
    const a = file('a.jpg', 'image/jpeg')
    const b = file('b.jpg', 'image/jpeg')
    const c = file('c.mp4', 'video/mp4')
    const hidden = file('.DS_Store', '')
    const out = await filesFromDrop(dt([
      fileEntry(a),
      dirEntry('Holiday', [fileEntry(b), fileEntry(hidden), dirEntry('Day 2', [fileEntry(c)])]),
    ]))
    expect(out.map(f => f.name)).toEqual(['a.jpg', 'b.jpg', 'c.mp4'])
  })

  it('stops at the drop cap', async () => {
    const many = Array.from({ length: MAX_DROP_FILES + 5 }, (_, i) => fileEntry(file(`${i}.jpg`, 'image/jpeg')))
    const out = await filesFromDrop(dt([dirEntry('All', many)]))
    expect(out).toHaveLength(MAX_DROP_FILES)
  })

  it('without the entries API it takes the plain file list', async () => {
    const a = file('a.jpg', 'image/jpeg')
    const plain = { types: ['Files'], files: [a], items: [{ kind: 'file' }] } as unknown as DataTransfer
    expect(await filesFromDrop(plain)).toEqual([a])
  })

  it('only a drag carrying files counts', () => {
    expect(dragHasFiles({ types: ['Files'] } as unknown as DataTransfer)).toBe(true)
    expect(dragHasFiles({ types: ['text/plain'] } as unknown as DataTransfer)).toBe(false)
    expect(dragHasFiles(null)).toBe(false)
  })
})

describe('formatSize', () => {
  it('reads as MB or KB', () => {
    expect(formatSize(2.4 * 1024 * 1024)).toBe('2.4 MB')
    expect(formatSize(24 * 1024 * 1024)).toBe('24 MB')
    expect(formatSize(300 * 1024)).toBe('300 KB')
  })
})

describe('household videos (5 minutes, up to 2 GB)', () => {
  it('a household video may be 2 GB; a photo or a space video 100 MB', () => {
    expect(maxBytes('video', true)).toBe(HOUSEHOLD_VIDEO_MAX_BYTES)
    expect(maxBytes('video', false)).toBe(MAX_UPLOAD_BYTES)
    expect(maxBytes('photo', true)).toBe(MAX_UPLOAD_BYTES)
    const big = file('birthday.mov', 'video/quicktime', 1.7 * 1024 ** 3)
    expect(checkFile(big, true).problem).toBeNull()
    expect(checkFile(big).problem).toBe('too_large')
    expect(checkFile(file('a.mp4', 'video/mp4', HOUSEHOLD_VIDEO_MAX_BYTES + 1), true).problem)
      .toBe('too_large')
  })

  it('five minutes is what a phone shows as 5:00', () => {
    expect(videoTooLong(300)).toBe(false)
    expect(videoTooLong(300.9)).toBe(false)
    expect(videoTooLong(301)).toBe(true)
    expect(videoTooLong(432)).toBe(true)
  })

  it('writes a length the way a phone shows it', () => {
    expect(formatDuration(432)).toBe('7:12')
    expect(formatDuration(59.9)).toBe('0:59')
    expect(formatDuration(3723)).toBe('1:02:03')
  })

  it('reads the length from the file, and gives up without blocking', async () => {
    const create = URL.createObjectURL
    const revoke = URL.revokeObjectURL
    URL.createObjectURL = vi.fn(() => 'blob:x')
    URL.revokeObjectURL = vi.fn()
    const realCreate = document.createElement.bind(document)
    let fire: 'meta' | 'error' | 'none' = 'meta'
    const spy = vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = realCreate(tag) as HTMLVideoElement
      if (tag === 'video') {
        Object.defineProperty(el, 'duration', { value: 432.4 })
        Object.defineProperty(el, 'src', {
          set() {
            setTimeout(() => {
              if (fire === 'meta') el.onloadedmetadata?.(new Event('loadedmetadata'))
              if (fire === 'error') el.onerror?.(new Event('error'))
            }, 0)
          },
          get() { return '' },
        })
      }
      return el
    })
    try {
      expect(await readVideoDuration(file('a.mov', 'video/quicktime'))).toBeCloseTo(432.4)
      fire = 'error'
      expect(await readVideoDuration(file('b.mov', 'video/quicktime'))).toBeNull()
      fire = 'none'
      expect(await readVideoDuration(file('c.mov', 'video/quicktime'), 20)).toBeNull()
      expect(URL.revokeObjectURL).toHaveBeenCalledTimes(3)
    } finally {
      spy.mockRestore()
      URL.createObjectURL = create
      URL.revokeObjectURL = revoke
    }
  })
})
