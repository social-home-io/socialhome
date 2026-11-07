import { describe, it, expect, afterEach } from 'vitest'
import type { JSX } from 'preact'
import { render, cleanup } from '@testing-library/preact'
import { SpaceGlyph } from './SpaceGlyph'
import { HA_SPACE_SYMBOL } from './spaceGlyph'

afterEach(cleanup)

const tile = (el: JSX.Element) =>
  render(<span data-testid="tile">{el}</span>).getByTestId('tile')

describe('SpaceGlyph — picture, then Home Assistant / emoji, then initial', () => {
  it('the uploaded picture wins over everything', () => {
    const el = tile(<SpaceGlyph iconUrl="api/spaces/s1/icon?v=1" emoji={HA_SPACE_SYMBOL} name="Home" />)
    const img = el.querySelector('img')!
    expect(img.getAttribute('src')).toBe('api/spaces/s1/icon?v=1')
    expect(img.getAttribute('alt')).toBe('')
    expect(el.querySelector('svg')).toBeNull()
  })

  it('no picture: the Home Assistant symbol draws the logo, not a house', () => {
    const el = tile(<SpaceGlyph emoji={HA_SPACE_SYMBOL} name="Home" />)
    const svg = el.querySelector('svg.sh-ha-logo')!
    expect(svg.getAttribute('aria-hidden')).toBe('true')
    expect(svg.querySelector('path')!.getAttribute('fill')).toBe('currentColor')
    expect(el.textContent).toBe('')
  })

  it('a picked house stays a house', () => {
    const el = tile(<SpaceGlyph emoji="🏠" name="Home" />)
    expect(el.querySelector('svg')).toBeNull()
    expect(el.textContent).toBe('🏠')
  })

  it('no picture, no emoji: the initial', () => {
    expect(tile(<SpaceGlyph iconUrl={null} emoji={null} name="garden" />).textContent).toBe('G')
  })
})
