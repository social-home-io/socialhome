import { describe, it, expect, afterEach, vi } from 'vitest'
import { setLocale } from '@/i18n/i18n'
import { formatSeconds, readVideoDurationMs } from './videoDuration'

describe('formatSeconds', () => {
  afterEach(async () => { await setLocale('en') })

  it('is one decimal in the UI language, never a hard-coded "." ', async () => {
    expect(formatSeconds(2_450)).toBe('2.5')
    await setLocale('de')
    expect(formatSeconds(2_450)).toBe('2,5')
  })
})

describe('readVideoDurationMs', () => {
  afterEach(() => { vi.restoreAllMocks() })

  function fakeVideo(outcome: 'ok' | 'error') {
    const real = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = real(tag)
      if (tag === 'video') {
        Object.defineProperty(el, 'duration', { value: 12.3 })
        Object.defineProperty(el, 'src', {
          set() {
            queueMicrotask(() => {
              const v = el as HTMLVideoElement
              if (outcome === 'ok') v.onloadedmetadata?.(new Event('loadedmetadata'))
              else v.onerror?.(new Event('error'))
            })
          },
        })
      }
      return el
    })
    URL.createObjectURL = vi.fn(() => 'blob:x')
    URL.revokeObjectURL = vi.fn()
  }

  it('reads the length in ms', async () => {
    fakeVideo('ok')
    expect(await readVideoDurationMs(new File([''], 'a.mp4'))).toBe(12_300)
  })

  it('is null for a file the browser cannot read — no English error leaks out', async () => {
    fakeVideo('error')
    expect(await readVideoDurationMs(new File([''], 'a.mp4'))).toBeNull()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:x')
  })
})
