/**
 * PendingReviewStrip — the author's own items waiting for review on a
 * space tab: fetched from ``/moderation/mine``, filtered to the tab's
 * feature and to ``pending``, counted, and refetched on a
 * ``space.moderation.mine`` frame for the space.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'
import type { ModerationItem } from '@/features/spaces/moderationItems'

const apiGet = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => apiGet(...a) } }))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      if (!handlers.has(type)) handlers.set(type, new Set())
      handlers.get(type)!.add(h)
      return () => { handlers.get(type)?.delete(h) }
    },
  },
}))
const emit = (type: string, data: Record<string, unknown>) =>
  handlers.get(type)?.forEach(h => h({ type, data }))

import { PendingReviewStrip } from './PendingReviewStrip'
import { resetModerationMine, useModerationMine } from '@/store/moderationMine'

/** Matches the strip heading by its full text (label + count span). */
const heading = (text: string) => (_: string, el: Element | null) =>
  el?.tagName === 'H3' && (el.textContent ?? '').replace(/\s+/g, ' ').trim() === text


const item = (id: string, over: Partial<ModerationItem> = {}): ModerationItem => ({
  id, space_id: 'sp-1', feature: 'tasks', action: 'create', entity: 'task', op: null,
  target_id: `t-${id}`, submitted_by: 'me', submitted_at: '2026-10-01T00:00:00Z',
  expires_at: '2099-01-01T00:00:00Z', status: 'pending',
  preview: { title: `Task ${id}` }, snapshot: null, current: null,
  ...over,
})

function Harness({ feature = 'tasks' }: { feature?: string }) {
  useModerationMine('sp-1')
  return <PendingReviewStrip spaceId="sp-1" feature={feature} />
}

beforeEach(() => {
  apiGet.mockReset()
  handlers.clear()
  resetModerationMine()
})

describe('PendingReviewStrip', () => {
  it('shows the caller\'s pending items of the tab\'s feature, with a count and badge', async () => {
    apiGet.mockResolvedValue([
      item('a'),
      item('b', { action: 'edit', preview: { title: 'Renamed' } }),
      item('c', { status: 'rejected' }),
      item('d', { feature: 'stickies', entity: 'sticky', preview: { content: 'note' } }),
    ])
    const view = render(<Harness />)
    expect(await view.findByText(heading('Pending review (2)'))).toBeTruthy()
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/sp-1/moderation/mine')
    expect(view.getByText('Task a')).toBeTruthy()
    expect(view.getByText('Renamed')).toBeTruthy()
    expect(view.getByText('New task')).toBeTruthy()
    expect(view.getByText('Edit to task')).toBeTruthy()
    expect(view.getAllByText('Awaiting review')).toHaveLength(2)
    // A decided item and another feature's item stay out.
    expect(view.queryByText('note')).toBeNull()
  })

  it('renders nothing without pending items', async () => {
    apiGet.mockResolvedValue([item('c', { status: 'approved' })])
    const view = render(<Harness />)
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(view.container.querySelector('.sh-pending-review')).toBeNull()
  })

  it('refetches on a space.moderation.mine frame for this space only', async () => {
    apiGet.mockResolvedValueOnce([item('a')])
    const view = render(<Harness />)
    expect(await view.findByText(heading('Pending review (1)'))).toBeTruthy()

    emit('space.moderation.mine', { space_id: 'sp-2', item_id: 'x', status: 'pending' })
    expect(apiGet).toHaveBeenCalledTimes(1)

    // The moderator approved "a": the strip empties.
    apiGet.mockResolvedValueOnce([item('a', { status: 'approved' })])
    emit('space.moderation.mine', { space_id: 'sp-1', item_id: 'a', status: 'approved' })
    await waitFor(() => expect(view.queryByText(heading('Pending review (1)'))).toBeNull())
    expect(apiGet).toHaveBeenCalledTimes(2)
  })

  it('keeps what it shows when a refetch fails', async () => {
    apiGet.mockResolvedValueOnce([item('a')])
    const view = render(<Harness />)
    await view.findByText(heading('Pending review (1)'))
    apiGet.mockRejectedValueOnce(new Error('offline'))
    emit('space.moderation.mine', { space_id: 'sp-1' })
    await new Promise(r => setTimeout(r, 0))
    expect(view.getByText(heading('Pending review (1)'))).toBeTruthy()
  })

  // Regression: "Pending review (1)" over four rows. The count and the
  // list share one source; the display face's ss01 "4" read as "1", so
  // the count now sits in its own body-face span with lining figures.
  it('counts exactly the rows it lists, past the collapse, in a body-face span', async () => {
    apiGet.mockResolvedValue(['a', 'b', 'c', 'd'].map(id => item(id)))
    const view = render(<Harness />)
    expect(await view.findByText(heading('Pending review (4)'))).toBeTruthy()
    const count = view.getByTestId('pending-review-count')
    expect(count.textContent).toBe('(4)')
    expect(count.classList.contains('sh-pending-review__count')).toBe(true)
    expect(view.container.querySelectorAll('.sh-pending-review__item')).toHaveLength(3)
    expect(view.getByText('Show all 4')).toBeTruthy()
    view.getByText('Show all 4').click()
    await waitFor(() =>
      expect(view.container.querySelectorAll('.sh-pending-review__item')).toHaveLength(4))
    expect(count.textContent).toBe('(4)')
  })

  it('counts per tab: each feature strip only its own pending items', async () => {
    apiGet.mockResolvedValue([
      item('p1', { feature: 'posts', entity: 'post', preview: { content: 'x' } }),
      item('p2', { feature: 'posts', entity: 'post', preview: { content: 'y', bazaar: { title: 'Bike' } } }),
      item('t1'),
      item('s1', { feature: 'stickies', entity: 'sticky', preview: { content: 'n' } }),
      item('c1', { feature: 'calendar', entity: 'event', preview: { summary: 'E' } }),
      item('c2', { feature: 'calendar', entity: 'event', preview: { summary: 'F' } }),
      item('c3', { feature: 'calendar', entity: 'event', preview: { summary: 'G' }, status: 'approved' }),
    ])
    function All() {
      useModerationMine('sp-1')
      return (
        <>
          {(['posts', 'tasks', 'stickies', 'calendar', 'pages'] as const).map(f => (
            <div data-testid={`strip-${f}`} key={f}><PendingReviewStrip spaceId="sp-1" feature={f} /></div>
          ))}
          <div data-testid="strip-bazaar">
            <PendingReviewStrip spaceId="sp-1" feature="posts" filter={i => !!i.preview?.bazaar} />
          </div>
        </>
      )
    }
    const view = render(<All />)
    const countIn = (id: string) =>
      view.getByTestId(id).querySelector('.sh-pending-review__count')?.textContent ?? null
    await waitFor(() => expect(countIn('strip-posts')).toBe('(2)'))
    expect(countIn('strip-tasks')).toBe('(1)')
    expect(countIn('strip-stickies')).toBe('(1)')
    expect(countIn('strip-calendar')).toBe('(2)')
    expect(countIn('strip-pages')).toBeNull()
    expect(countIn('strip-bazaar')).toBe('(1)')
  })
})
