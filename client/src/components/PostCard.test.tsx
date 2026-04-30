import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { PostCard } from './PostCard'
import type { FeedPost } from '@/types'

const mockPost: FeedPost = {
  id: 'p1',
  author: 'anna',
  type: 'text',
  content: 'Hello world!',
  media_url: null,
  image_urls: [],
  file_meta: null,
  reactions: { '👍': ['u1', 'u2'] },
  comment_count: 3,
  pinned: false,
  created_at: new Date().toISOString(),
  edited_at: null,
}

describe('PostCard', () => {
  it('renders post content', () => {
    const { getByText } = render(<PostCard post={mockPost} />)
    expect(getByText('Hello world!')).toBeTruthy()
  })

  it('shows reaction counts', () => {
    const { container } = render(<PostCard post={mockPost} />)
    expect(container.textContent).toContain('👍')
    expect(container.textContent).toContain('2')
  })

  it('shows comment count', () => {
    const { container } = render(<PostCard post={mockPost} />)
    expect(container.textContent).toContain('3')
  })

  it('shows pinned badge when pinned', () => {
    const pinned = { ...mockPost, pinned: true }
    const { container } = render(<PostCard post={pinned} />)
    expect(container.textContent).toContain('Pinned')
  })

  it('shows deleted state', () => {
    const deleted = { ...mockPost, content: null }
    const { container } = render(<PostCard post={deleted} />)
    expect(container.textContent).toContain('deleted')
  })

  it('shows edited badge', () => {
    const edited = { ...mockPost, edited_at: new Date().toISOString() }
    const { container } = render(<PostCard post={edited} />)
    expect(container.textContent).toContain('edited')
  })

  it('calls onReact when an existing reaction chip is clicked', () => {
    const fn = vi.fn()
    const { container } = render(<PostCard post={mockPost} onReact={fn} />)
    const chip = container.querySelector('.sh-reaction-chip')
    if (chip) fireEvent.click(chip)
    expect(fn).toHaveBeenCalledWith('👍')
  })

  it('opens the reaction picker when + is clicked, then forwards onReact', () => {
    const fn = vi.fn()
    const { container } = render(<PostCard post={mockPost} onReact={fn} />)
    const addBtn = container.querySelector('.sh-reaction-add') as HTMLButtonElement | null
    expect(addBtn).toBeTruthy()
    fireEvent.click(addBtn!)
    // Picker mounts inside the same wrapper.
    const picker = container.querySelector('.sh-reaction-picker')
    expect(picker).toBeTruthy()
    // Picking an emoji from the frequent strip dispatches onReact.
    const firstFrequent = picker!.querySelector('.sh-reaction-frequent .sh-emoji-btn') as HTMLButtonElement | null
    expect(firstFrequent).toBeTruthy()
    fireEvent.click(firstFrequent!)
    expect(fn).toHaveBeenCalledTimes(1)
    // Picker closes after selection.
    expect(container.querySelector('.sh-reaction-picker')).toBeNull()
  })

  it('calls onComment when comment button clicked', () => {
    const fn = vi.fn()
    const { container } = render(<PostCard post={mockPost} onComment={fn} />)
    const btn = container.querySelector('.sh-comment-btn')
    if (btn) fireEvent.click(btn)
    expect(fn).toHaveBeenCalledOnce()
  })

  // ── Bot-bridge posts ────────────────────────────────────────────────────

  const mockBotPost: FeedPost = {
    ...mockPost,
    author: 'system-integration',
    content: '**Ring**\nFront door',
    bot: {
      bot_id: 'b1',
      scope: 'space',
      name: 'Doorbell',
      icon: '🔔',
      created_by_display_name: 'Alice',
    },
  }

  it('renders bot name in place of author for bot posts', () => {
    const { container } = render(<PostCard post={mockBotPost} />)
    expect(container.textContent).toContain('Doorbell')
  })

  it('renders bot icon via BotAvatar', () => {
    const { container } = render(<PostCard post={mockBotPost} />)
    expect(container.querySelector('.sh-bot-avatar')).toBeTruthy()
    expect(container.textContent).toContain('🔔')
  })

  it('renders "via Home Assistant" for scope=space bots', () => {
    const { container } = render(<PostCard post={mockBotPost} />)
    expect(container.textContent).toContain('via Home Assistant')
  })

  it('renders "via {member}" for scope=member bots', () => {
    const memberBot = {
      ...mockBotPost,
      bot: { ...mockBotPost.bot!, scope: 'member' as const },
    }
    const { container } = render(<PostCard post={memberBot} />)
    expect(container.textContent).toContain('via Alice')
  })

  it('falls back to HA when bot has been deleted (bot=null)', () => {
    const orphaned = { ...mockBotPost, bot: null }
    const { container } = render(<PostCard post={orphaned} />)
    expect(container.textContent).toContain('via Home Assistant')
  })

  it('hides reactions and comment button on bot posts', () => {
    const { container } = render(<PostCard post={mockBotPost} />)
    expect(container.querySelector('.sh-reaction-add')).toBeNull()
    expect(container.querySelector('.sh-comment-btn')).toBeNull()
  })
})
