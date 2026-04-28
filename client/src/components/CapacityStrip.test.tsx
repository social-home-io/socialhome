import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'

// No i18n mock — the real `t()` falls back to en.json keys, which gives
// us human-readable strings like "5/5 going" so assertions stay
// readable. If en.json drifts, the test surfaces it.
import { CapacityStrip } from './CapacityStrip'

describe('CapacityStrip', () => {
  it('renders empty state when no counts present', () => {
    const { container } = render(<CapacityStrip counts={undefined} />)
    expect(container.textContent).toContain('Be the first to RSVP')
  })

  it('renders the going segment even when count is zero', () => {
    const { container } = render(
      <CapacityStrip counts={{ going: 0, maybe: 0, declined: 0 }} />,
    )
    expect(container.textContent).toContain('0 going')
  })

  it('skips zero non-going segments to reduce noise', () => {
    const { container } = render(
      <CapacityStrip counts={{ going: 4, maybe: 0, declined: 0 }} />,
    )
    expect(container.textContent).toContain('4 going')
    expect(container.textContent).not.toContain('declined')
    expect(container.textContent).not.toContain('maybe')
  })

  it('renders going_with_cap when capacity is set', () => {
    const { container } = render(
      <CapacityStrip
        counts={{ going: 4, maybe: 0, declined: 0 }}
        capacity={20}
      />,
    )
    expect(container.textContent).toContain('4/20 going')
  })

  it('highlights the current user segment with --mine class', () => {
    const { container } = render(
      <CapacityStrip
        counts={{ going: 1, maybe: 0, declined: 0 }}
        myStatus="going"
      />,
    )
    expect(container.querySelector('.sh-capacity-segment--mine')).toBeTruthy()
  })

  it('renders requested + waitlist segments when populated', () => {
    const { container } = render(
      <CapacityStrip
        counts={{
          going: 5, maybe: 1, declined: 0, requested: 3, waitlist: 2,
        }}
        capacity={5}
      />,
    )
    expect(container.textContent).toContain('5/5 going')
    expect(container.textContent).toContain('2 waitlist')
    expect(container.textContent).toContain('3 pending')
    expect(container.textContent).toContain('1 maybe')
  })
})
