import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { LocalAreaBadge, localAreaVerdictText, verdictAllowsJoin } from './LocalAreaBadge'

describe('LocalAreaBadge', () => {
  it('says where our home is for one of our own local communities', () => {
    expect(localAreaVerdictText('inside')).toBe('In your area')
    expect(localAreaVerdictText('outside')).toBe('Outside your area')
    expect(localAreaVerdictText('no_home')).toBe("Your home location isn't set")
    // Near the edge an admin decides — the owner's 2026-10-19 wording.
    expect(localAreaVerdictText('edge')).toBe('Near the edge — an admin will review your request')
  })

  it('says nothing while the home is loading', () => {
    const { container } = render(<LocalAreaBadge verdict="unknown" />)
    expect(container.textContent).toBe('')
  })

  it('renders a chip per verdict', () => {
    const { getByTestId } = render(<LocalAreaBadge verdict="outside" />)
    const chip = getByTestId('local-area-badge')
    expect(chip.className).toContain('sh-local-area-badge--outside')
    expect(chip.textContent).toContain('Outside your area')
  })

  it('inside, near the edge (an admin reviews it) or not yet known offer a join', () => {
    expect(verdictAllowsJoin('inside')).toBe(true)
    expect(verdictAllowsJoin('unknown')).toBe(true)
    expect(verdictAllowsJoin('edge')).toBe(true)
    expect(verdictAllowsJoin('outside')).toBe(false)
    expect(verdictAllowsJoin('no_home')).toBe(false)
  })
})
