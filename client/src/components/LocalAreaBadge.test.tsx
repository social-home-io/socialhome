import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { LocalAreaBadge, localAreaVerdictText, verdictAllowsJoin } from './LocalAreaBadge'

describe('LocalAreaBadge', () => {
  it('says where the home is, naming the host when sharing is missing', () => {
    expect(localAreaVerdictText('inside')).toBe('In your area')
    expect(localAreaVerdictText('outside')).toBe('Outside your area')
    expect(localAreaVerdictText('no_home')).toBe("Your home location isn't set")
    expect(localAreaVerdictText('not_shared', 'Sara’s household'))
      .toBe('Share your home with Sara’s household to check')
    expect(localAreaVerdictText('not_shared', null)).toBe('Share your home with its household to check')
    expect(localAreaVerdictText('edge')).toBe('Near the edge — ask to join and the host will check')
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

  it('only inside (or not yet known) offers a join', () => {
    expect(verdictAllowsJoin('inside')).toBe(true)
    expect(verdictAllowsJoin('unknown')).toBe(true)
    // Near the edge the host decides on the exact distance: let them ask.
    expect(verdictAllowsJoin('edge')).toBe(true)
    expect(verdictAllowsJoin('outside')).toBe(false)
    expect(verdictAllowsJoin('no_home')).toBe(false)
    expect(verdictAllowsJoin('not_shared')).toBe(false)
  })
})
