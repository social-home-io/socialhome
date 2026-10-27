import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/preact'
import {
  PlaybackControls,
  isRtl,
  prefersReducedMotion,
  usePlaybackKeys,
  type PlaybackActions,
} from './viewer_playback'

afterEach(() => {
  cleanup()
  document.documentElement.removeAttribute('dir')
  vi.unstubAllGlobals()
})

function Keys({ actions }: { actions: PlaybackActions }) {
  usePlaybackKeys(actions)
  return <button type="button">elsewhere</button>
}

function spies(): Required<PlaybackActions> {
  return {
    togglePause: vi.fn(),
    toggleMute: vi.fn(),
    prev: vi.fn(),
    next: vi.fn(),
  }
}

describe('PlaybackControls', () => {
  it('names every button for screen readers and says what pressing it does', () => {
    const a = spies()
    render(
      <PlaybackControls
        paused={false}
        muted
        onTogglePause={a.togglePause}
        onToggleMute={a.toggleMute}
        onPrev={a.prev}
        onNext={a.next}
      />,
    )
    expect(screen.getByRole('group', { name: 'Playback' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Previous' }))
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    fireEvent.click(screen.getByRole('button', { name: 'Unmute' }))
    expect(a.prev).toHaveBeenCalledOnce()
    expect(a.togglePause).toHaveBeenCalledOnce()
    expect(a.next).toHaveBeenCalledOnce()
    expect(a.toggleMute).toHaveBeenCalledOnce()
  })

  it('offers Play while paused and Mute while the sound is on', () => {
    render(
      <PlaybackControls paused muted={false} onTogglePause={() => {}} onToggleMute={() => {}} />,
    )
    expect(screen.getByRole('button', { name: 'Play' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Mute' })).toBeTruthy()
  })

  it('leaves out previous / next / mute when there is nothing to do with them', () => {
    render(<PlaybackControls paused={false} muted={null} onTogglePause={() => {}} />)
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })

  it('mirrors the previous / next arrows in a right-to-left page', () => {
    const { container } = render(
      <PlaybackControls paused={false} muted={null} onTogglePause={() => {}} onPrev={() => {}} onNext={() => {}} />,
    )
    // The CSS flips every ``.viewer-flip`` icon under ``[dir=rtl]``.
    expect(container.querySelectorAll('.viewer-flip')).toHaveLength(2)
  })
})

describe('usePlaybackKeys', () => {
  it('Space and K pause, M mutes, the arrows step', () => {
    const a = spies()
    render(<Keys actions={a} />)
    fireEvent.keyDown(document.body, { key: ' ' })
    fireEvent.keyDown(document.body, { key: 'k' })
    fireEvent.keyDown(document.body, { key: 'M' })
    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    fireEvent.keyDown(document.body, { key: 'ArrowLeft' })
    expect(a.togglePause).toHaveBeenCalledTimes(2)
    expect(a.toggleMute).toHaveBeenCalledOnce()
    expect(a.next).toHaveBeenCalledOnce()
    expect(a.prev).toHaveBeenCalledOnce()
  })

  it('swaps the arrows in a right-to-left page', () => {
    document.documentElement.setAttribute('dir', 'rtl')
    const a = spies()
    render(<Keys actions={a} />)
    fireEvent.keyDown(document.body, { key: 'ArrowLeft' })
    expect(a.next).toHaveBeenCalledOnce()
    expect(a.prev).not.toHaveBeenCalled()
    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    expect(a.prev).toHaveBeenCalledOnce()
  })

  it('leaves Space to a focused button, and ignores shortcuts with a modifier', () => {
    const a = spies()
    render(<Keys actions={a} />)
    fireEvent.keyDown(screen.getByRole('button', { name: 'elsewhere' }), { key: ' ' })
    fireEvent.keyDown(document.body, { key: 'k', ctrlKey: true })
    fireEvent.keyDown(document.body, { key: 'ArrowRight', metaKey: true })
    expect(a.togglePause).not.toHaveBeenCalled()
    expect(a.next).not.toHaveBeenCalled()
  })

  it('stops listening once unmounted', () => {
    const a = spies()
    const view = render(<Keys actions={a} />)
    act(() => { view.unmount() })
    fireEvent.keyDown(document.body, { key: 'k' })
    expect(a.togglePause).not.toHaveBeenCalled()
  })
})

describe('motion and direction', () => {
  it('reads prefers-reduced-motion, and assumes motion is fine without matchMedia', () => {
    vi.stubGlobal('matchMedia', undefined)
    expect(prefersReducedMotion()).toBe(false)
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('reduce') }))
    expect(prefersReducedMotion()).toBe(true)
  })

  it('reads the page direction', () => {
    expect(isRtl()).toBe(false)
    document.documentElement.setAttribute('dir', 'rtl')
    expect(isRtl()).toBe(true)
  })
})
