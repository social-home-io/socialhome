import { describe, it, expect, afterEach, vi } from 'vitest'
import { setLocale } from '@/i18n/i18n'
import { PROBE_TIMEOUT_MS, formatSeconds, readVideoDurationMs } from './videoDuration'

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

  function fakeVideo(outcome: 'ok' | 'error' | 'hang' | 'infinite') {
    const real = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = real(tag)
      if (tag === 'video') {
        Object.defineProperty(el, 'duration', { value: outcome === 'infinite' ? Infinity : 12.3 })
        Object.defineProperty(el, 'src', {
          set() {
            queueMicrotask(() => {
              const v = el as HTMLVideoElement
              if (outcome === 'hang') return
              if (outcome === 'error') v.onerror?.(new Event('error'))
              else v.onloadedmetadata?.(new Event('loadedmetadata'))
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

  it('is null when the browser reports no finite length (some WebM files)', async () => {
    fakeVideo('infinite')
    expect(await readVideoDurationMs(new File([''], 'a.webm'))).toBeNull()
  })

  it('gives up on a probe that never answers, instead of leaving the composer busy (B10)', async () => {
    vi.useFakeTimers()
    try {
      fakeVideo('hang')
      const pending = readVideoDurationMs(new File([''], 'a.mp4'))
      await vi.advanceTimersByTimeAsync(PROBE_TIMEOUT_MS + 1)
      expect(await pending).toBeNull()
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:x')
    } finally {
      vi.useRealTimers()
    }
  })
})
