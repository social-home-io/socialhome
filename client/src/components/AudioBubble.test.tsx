import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { afterEach, describe, it, expect, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/preact'
import { AudioBubble, formatVoiceTime } from './AudioBubble'

/** jsdom has no media pipeline: give one element a duration. */
function setDuration(audio: HTMLAudioElement, value: number) {
  Object.defineProperty(audio, 'duration', { configurable: true, value })
}

describe('AudioBubble', () => {
  it('renders an audio element with the src + preload="metadata"', () => {
    const { container } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" />,
    )
    const audio = container.querySelector('audio')
    expect(audio).toBeTruthy()
    expect(audio?.getAttribute('src')).toBe('api/media/v.ogg')
    expect(audio?.getAttribute('preload')).toBe('metadata')
  })

  it('shows a "Transcribing…" placeholder when transcript is empty', () => {
    const { getByText } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" />,
    )
    expect(getByText('Transcribing…')).toBeTruthy()
  })

  it('renders the transcript text when present', () => {
    const { getByText, queryByText } = render(
      <AudioBubble src="api/media/v.ogg" transcript="hello there" />,
    )
    expect(getByText('hello there')).toBeTruthy()
    expect(queryByText('Transcribing…')).toBeNull()
  })

  it('exposes the filename as aria-label on the player', () => {
    const { container } = render(
      <AudioBubble
        src="api/media/v.ogg"
        transcript=""
        fileName="voice-note-123.ogg"
      />,
    )
    expect(container.querySelector('audio')?.getAttribute('aria-label')).toBe(
      'voice-note-123.ogg',
    )
  })

  it('falls back to "Voice note" when no filename is supplied', () => {
    const { container } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" />,
    )
    expect(container.querySelector('audio')?.getAttribute('aria-label')).toBe(
      'Voice note',
    )
  })

  it('adds the pending modifier class when sync is in-flight', () => {
    const { container } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" pending />,
    )
    expect(
      container.querySelector('.sh-message-audio__player--pending'),
    ).toBeTruthy()
  })

  // Regression: Chrome's ``<audio controls>`` puts Download and Playback
  // speed behind a ⋮ menu that opens upward from the button; for a voice
  // note near the top of a phone screen the speed list ran off the top
  // and couldn't be reached. The bubble owns its controls instead.
  it('never uses the native controls (their ⋮ menu ran off a phone screen)', () => {
    const { container } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" />,
    )
    expect(container.querySelector('audio')?.hasAttribute('controls')).toBe(false)
    expect(container.querySelector('.sh-message-audio__play')).toBeTruthy()
    expect(container.querySelector('.sh-message-audio__seek')).toBeTruthy()
    expect(container.querySelector('.sh-message-audio__speed')).toBeTruthy()
  })

  describe('controls', () => {
    afterEach(() => { vi.restoreAllMocks() })

    it('plays and pauses from the play button', async () => {
      const play = vi.spyOn(HTMLMediaElement.prototype, 'play')
        .mockImplementation(function (this: HTMLMediaElement) {
          this.dispatchEvent(new Event('play'))
          return Promise.resolve()
        })
      const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause')
        .mockImplementation(function (this: HTMLMediaElement) {
          this.dispatchEvent(new Event('pause'))
        })
      const { container, getByRole } = render(
        <AudioBubble src="api/media/v.ogg" transcript="" />,
      )
      fireEvent.click(getByRole('button', { name: 'Play voice note' }))
      expect(play).toHaveBeenCalledTimes(1)
      expect(
        container.querySelector('.sh-message-audio__play')?.getAttribute('aria-label'),
      ).toBe('Pause voice note')
      // jsdom keeps ``paused`` true; pretend the element is playing.
      Object.defineProperty(container.querySelector('audio')!, 'paused', {
        configurable: true, value: false,
      })
      fireEvent.click(getByRole('button', { name: 'Pause voice note' }))
      expect(pause).toHaveBeenCalledTimes(1)
      expect(getByRole('button', { name: 'Play voice note' })).toBeTruthy()
    })

    it('cycles the playback speed 1× → 1.5× → 2× → 1×', () => {
      const { container } = render(
        <AudioBubble src="api/media/v.ogg" transcript="" />,
      )
      const audio = container.querySelector('audio')!
      const chip = container.querySelector('.sh-message-audio__speed') as HTMLButtonElement
      expect(chip.textContent).toBe('1×')
      fireEvent.click(chip)
      expect(audio.playbackRate).toBe(1.5)
      expect(chip.textContent).toBe('1.5×')
      expect(chip.getAttribute('aria-label')).toBe('Playback speed: 1.5×')
      fireEvent.click(chip)
      expect(audio.playbackRate).toBe(2)
      fireEvent.click(chip)
      expect(audio.playbackRate).toBe(1)
      expect(chip.textContent).toBe('1×')
    })

    it('shows the length once known and seeks from the slider', () => {
      const { container } = render(
        <AudioBubble src="api/media/v.ogg" transcript="" />,
      )
      const audio = container.querySelector('audio')!
      const seek = container.querySelector('.sh-message-audio__seek') as HTMLInputElement
      expect(seek.disabled).toBe(true)
      setDuration(audio, 65)
      fireEvent(audio, new Event('loadedmetadata'))
      expect(seek.disabled).toBe(false)
      expect(seek.max).toBe('65')
      expect(container.querySelector('.sh-message-audio__time')?.textContent).toBe('1:05')
      fireEvent.input(seek, { target: { value: '30' } })
      expect(audio.currentTime).toBe(30)
      expect(container.querySelector('.sh-message-audio__time')?.textContent).toBe('0:30 / 1:05')
    })

    it('works out the length of a recording without one (MediaRecorder WebM)', () => {
      const { container } = render(
        <AudioBubble src="api/media/v.webm" transcript="" />,
      )
      const audio = container.querySelector('audio')!
      setDuration(audio, Infinity)
      fireEvent(audio, new Event('loadedmetadata'))
      // Seeking far past the end makes the browser compute the length.
      expect(audio.currentTime).toBe(1e101)
      // That probe's time update must not move the slider.
      fireEvent(audio, new Event('timeupdate'))
      expect(container.querySelector('.sh-message-audio__time')?.textContent).toBe('0:00')
      setDuration(audio, 7.4)
      fireEvent(audio, new Event('durationchange'))
      expect(audio.currentTime).toBe(0)
      expect(container.querySelector('.sh-message-audio__time')?.textContent).toBe('0:07')
    })

    it('rewinds the time when the note ends', () => {
      const { container } = render(
        <AudioBubble src="api/media/v.ogg" transcript="" />,
      )
      const audio = container.querySelector('audio')!
      setDuration(audio, 10)
      fireEvent(audio, new Event('loadedmetadata'))
      audio.currentTime = 10
      fireEvent(audio, new Event('timeupdate'))
      fireEvent(audio, new Event('ended'))
      expect(container.querySelector('.sh-message-audio__time')?.textContent).toBe('0:10')
    })
  })

  it('offers a download link for a local voice note', () => {
    const { container } = render(
      <AudioBubble src="api/media/v.ogg" transcript="" fileName="note.ogg" />,
    )
    const link = container.querySelector('a.sh-message-audio__download')
    expect(link?.getAttribute('href')).toBe('api/media/v.ogg')
    expect(link?.getAttribute('download')).toBe('note.ogg')
    expect(link?.getAttribute('aria-label')).toBe('Download')
  })

  it('leaves the download link out for a script URL', () => {
    const { container } = render(
      <AudioBubble src="javascript:alert(1)" transcript="" />,
    )
    expect(container.querySelector('a.sh-message-audio__download')).toBeNull()
  })
})

describe('formatVoiceTime', () => {
  it('formats m:ss and treats unknown lengths as zero', () => {
    expect(formatVoiceTime(0)).toBe('0:00')
    expect(formatVoiceTime(7.9)).toBe('0:07')
    expect(formatVoiceTime(125)).toBe('2:05')
    expect(formatVoiceTime(Infinity)).toBe('0:00')
    expect(formatVoiceTime(Number.NaN)).toBe('0:00')
  })
})

// Guard: no ``<audio controls>`` anywhere in the SPA — the native ⋮ menu
// can't be positioned by page CSS (see AudioBubble.tsx).
describe('no native audio controls in the SPA', () => {
  it('has no <audio> with a controls attribute', () => {
    const root = join(__dirname, '..')
    const offenders: string[] = []
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name)
        if (statSync(p).isDirectory()) walk(p)
        else if (p.endsWith('.tsx') && !p.endsWith('.test.tsx')) {
          // Comments may name the old ``<audio controls>``; code may not.
          const src = readFileSync(p, 'utf8')
            .replace(/\/\*[\s\S]*?\*\//g, '')
            .replace(/^\s*\/\/.*$/gm, '')
          for (const m of src.matchAll(/<audio\b[^>]*>/g)) {
            if (/\scontrols\b/.test(m[0])) offenders.push(`${relative(root, p)}: ${m[0]}`)
          }
        }
      }
    }
    walk(root)
    expect(offenders).toEqual([])
  })
})
