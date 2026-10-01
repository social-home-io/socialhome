import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'

import {
  Skeleton,
  PostCardSkeleton,
  FeedSkeleton,
  DmInboxSkeleton,
  HighlightsRingSkeleton,
  CalendarSkeleton,
  SpaceListSkeleton,
  BazaarSkeleton,
  CommentThreadSkeleton,
  NotificationListSkeleton,
  DmThreadSkeleton,
  ListSkeleton,
} from './Skeleton'

describe('Skeleton primitive', () => {
  it('renders with the default rect shape and aria-busy', () => {
    const { container } = render(<Skeleton />)
    const el = container.querySelector('.sh-skeleton') as HTMLElement
    expect(el).toBeTruthy()
    expect(el.classList.contains('sh-skeleton--rect')).toBe(true)
    expect(el.getAttribute('aria-busy')).toBe('true')
  })

  it('honours the shape prop', () => {
    const { container } = render(<Skeleton shape="circle" />)
    expect(container.querySelector('.sh-skeleton--circle')).toBeTruthy()
  })

  it('applies the inline width / height as pixel strings', () => {
    const { container } = render(<Skeleton width={80} height={20} />)
    const el = container.querySelector('.sh-skeleton') as HTMLElement
    expect(el.style.width).toBe('80px')
    expect(el.style.height).toBe('20px')
  })

  it('passes through string width values verbatim (e.g. "40%")', () => {
    const { container } = render(<Skeleton width="40%" />)
    const el = container.querySelector('.sh-skeleton') as HTMLElement
    expect(el.style.width).toBe('40%')
  })
})

describe('Page-shaped skeletons', () => {
  it('PostCardSkeleton renders a header + body', () => {
    const { container } = render(<PostCardSkeleton />)
    expect(container.querySelector('.sh-post--skeleton')).toBeTruthy()
    expect(container.querySelectorAll('.sh-skeleton').length).toBeGreaterThan(3)
  })

  it('PostCardSkeleton renders a media block when withMedia', () => {
    const { container: a } = render(<PostCardSkeleton />)
    const { container: b } = render(<PostCardSkeleton withMedia />)
    // The media-bearing variant has at least one more skeleton block
    // than the bare variant.
    expect(b.querySelectorAll('.sh-skeleton').length)
      .toBeGreaterThan(a.querySelectorAll('.sh-skeleton').length)
  })

  it('FeedSkeleton renders 3 post-shaped skeletons', () => {
    const { container } = render(<FeedSkeleton />)
    expect(container.querySelectorAll('.sh-post--skeleton').length).toBe(3)
  })

  it('DmInboxSkeleton renders 5 inbox rows', () => {
    const { container } = render(<DmInboxSkeleton />)
    expect(container.querySelectorAll('.sh-dm-inbox-row').length).toBe(5)
  })

  it('HighlightsRingSkeleton renders 5 ring placeholders', () => {
    const { container } = render(<HighlightsRingSkeleton />)
    expect(container.querySelectorAll('.sh-highlight-ring').length).toBe(5)
  })

  it('CalendarSkeleton renders a 35-cell month grid', () => {
    const { container } = render(<CalendarSkeleton />)
    expect(
      container.querySelectorAll('.sh-skeleton-calendar-day').length,
    ).toBe(35)
  })

  it('SpaceListSkeleton defaults to 4 cards', () => {
    const { container } = render(<SpaceListSkeleton />)
    expect(
      container.querySelectorAll('.sh-space-card--skeleton').length,
    ).toBe(4)
  })

  it('SpaceListSkeleton honours the count prop', () => {
    const { container } = render(<SpaceListSkeleton count={6} />)
    expect(
      container.querySelectorAll('.sh-space-card--skeleton').length,
    ).toBe(6)
  })

  it('BazaarSkeleton renders 3 listing-card placeholders', () => {
    const { container } = render(<BazaarSkeleton />)
    expect(
      container.querySelectorAll('.sh-bazaar-card--skeleton').length,
    ).toBe(3)
  })

  it('CommentThreadSkeleton defaults to 3 comment rows', () => {
    const { container } = render(<CommentThreadSkeleton />)
    expect(
      container.querySelectorAll('.sh-comment-item--skeleton').length,
    ).toBe(3)
  })

  it('CommentThreadSkeleton honours the count prop', () => {
    const { container } = render(<CommentThreadSkeleton count={5} />)
    expect(
      container.querySelectorAll('.sh-comment-item--skeleton').length,
    ).toBe(5)
  })

  it('NotificationListSkeleton defaults to 5 rows', () => {
    const { container } = render(<NotificationListSkeleton />)
    expect(
      container.querySelectorAll('.sh-notif-row--skeleton').length,
    ).toBe(5)
  })

  it('NotificationListSkeleton honours the count prop', () => {
    const { container } = render(<NotificationListSkeleton count={3} />)
    expect(
      container.querySelectorAll('.sh-notif-row--skeleton').length,
    ).toBe(3)
  })

  it('DmThreadSkeleton defaults to 4 message bubbles', () => {
    const { container } = render(<DmThreadSkeleton />)
    expect(
      container.querySelectorAll('.sh-message--skeleton').length,
    ).toBe(4)
  })

  it('DmThreadSkeleton alternates --mine on every other row', () => {
    const { container } = render(<DmThreadSkeleton count={4} />)
    const mine = container.querySelectorAll('.sh-message--mine.sh-message--skeleton')
    expect(mine.length).toBe(2)
  })
})

describe('ListSkeleton', () => {
  it('is ONE busy region with ONE status label — no per-bone status', () => {
    const { container } = render(<ListSkeleton />)
    const busy = container.querySelectorAll('[aria-busy="true"]')
    expect(busy.length).toBe(1)
    const status = container.querySelectorAll('[role="status"]')
    expect(status.length).toBe(1)
    expect(status[0].textContent).toBe('Loading...')
    expect(status[0].className).toBe('sr-only')
    // The status line is a sibling of the busy art, not inside it —
    // assistive tech skips announcing content under aria-busy.
    expect(busy[0].contains(status[0])).toBe(false)
    expect(busy[0].classList.contains('sh-list-skeleton__art')).toBe(true)
    // No landmark (the page already has its <main>).
    expect(container.querySelector('main')).toBeNull()
  })

  it('list variant renders the requested number of rows', () => {
    const { container } = render(<ListSkeleton rows={3} label="Loading shopping list" />)
    expect(container.querySelector('.sh-list-skeleton--list')).not.toBeNull()
    expect(container.querySelectorAll('.sh-list-skeleton__row').length).toBe(3)
    expect(container.querySelector('[role="status"]')!.textContent).toBe('Loading shopping list')
  })

  it('board variant renders three columns of cards', () => {
    const { container } = render(<ListSkeleton variant="board" />)
    expect(container.querySelectorAll('.sh-list-skeleton__column').length).toBe(3)
    expect(container.querySelectorAll('.sh-list-skeleton__card').length).toBeGreaterThan(3)
  })

  it('sticky variant renders note tiles', () => {
    const { container } = render(<ListSkeleton variant="sticky" rows={4} />)
    expect(container.querySelectorAll('.sh-list-skeleton__note').length).toBe(4)
  })

  it('keeps its decorative bones hidden from assistive tech', () => {
    const { container } = render(<ListSkeleton />)
    const art = container.querySelector('.sh-list-skeleton__art')!
    expect(art.getAttribute('aria-hidden')).toBe('true')
    expect(art.getAttribute('aria-busy')).toBe('true')
  })
})
