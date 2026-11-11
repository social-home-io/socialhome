import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { PostCard } from './PostCard'
import type { FeedPost } from '@/types'
import { spaceMembers } from '@/store/spaceMembers'
import { householdUsers } from '@/store/householdUsers'
import { currentUser } from '@/store/auth'

const openPersonSpy = vi.fn()
vi.mock('@/components/PersonSheet', () => ({
  openPerson: (...args: unknown[]) => openPersonSpy(...args),
}))

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

  const manyReactions = (): Record<string, string[]> => {
    const reactions: Record<string, string[]> = {}
    const emoji = ['😀', '😁', '😂', '🤣', '😃', '😄', '😅', '😆']
    emoji.forEach((e, i) => { reactions[e] = Array.from({ length: i + 1 }, (_, n) => `u${n}`) })
    return reactions
  }
  const chipTexts = (container: Element) => Array.from(
    container.querySelectorAll('.sh-reaction-chip:not(.sh-reaction-more)'),
  ).map((c) => c.textContent?.trim())

  it('shows the six most used reactions and folds the rest behind +k', () => {
    const { container } = render(<PostCard post={{ ...mockPost, reactions: manyReactions() }} />)
    // Most used first; the two least used are folded.
    expect(chipTexts(container)).toEqual(['😆 8', '😅 7', '😄 6', '😃 5', '🤣 4', '😂 3'])
    const more = container.querySelector('.sh-reaction-more') as HTMLButtonElement
    expect(more.textContent).toBe('+2')
    expect(more.getAttribute('aria-label')).toBe('Show 2 more reactions')
    expect(more.getAttribute('aria-expanded')).toBe('false')
  })

  it('opens the fold without losing keyboard focus, and folds again', () => {
    const { container } = render(<PostCard post={{ ...mockPost, reactions: manyReactions() }} />)
    const toggle = () => container.querySelector('.sh-reaction-more') as HTMLButtonElement
    const before = toggle()
    before.focus()
    fireEvent.click(before)
    expect(chipTexts(container)).toHaveLength(8)
    // The same control stays mounted and focused, now offering to fold.
    expect(toggle()).toBe(before)
    expect(document.activeElement).toBe(before)
    expect(toggle().getAttribute('aria-expanded')).toBe('true')
    expect(toggle().getAttribute('aria-label')).toBe('Show fewer reactions')
    fireEvent.click(toggle())
    expect(chipTexts(container)).toHaveLength(6)
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(before)
  })

  it('says "1 more reaction" in the singular', () => {
    const reactions = manyReactions()
    delete reactions['😀']
    const { container } = render(<PostCard post={{ ...mockPost, reactions }} />)
    const more = container.querySelector('.sh-reaction-more') as HTMLButtonElement
    expect(more.textContent).toBe('+1')
    expect(more.getAttribute('aria-label')).toBe('Show 1 more reaction')
  })

  it('never folds away a reaction the viewer made', () => {
    currentUser.value = { user_id: 'me', username: 'me', display_name: 'Me', is_admin: false } as never
    try {
      const reactions = { ...manyReactions(), '🙏': ['me'] }
      const { container } = render(<PostCard post={{ ...mockPost, reactions }} />)
      // The six most used, plus the viewer's own (the least used of all).
      expect(chipTexts(container)).toEqual(
        ['😆 8', '😅 7', '😄 6', '😃 5', '🤣 4', '😂 3', '🙏 1'],
      )
      expect(container.querySelector('.sh-reaction-more')?.textContent).toBe('+2')
    } finally {
      currentUser.value = null
    }
  })

  it('shows every reaction when there are at most six', () => {
    const reactions = { '😀': ['a'], '😁': ['a'], '😂': ['a', 'b'] }
    const { container } = render(<PostCard post={{ ...mockPost, reactions }} />)
    expect(container.querySelectorAll('.sh-reaction-chip')).toHaveLength(3)
    expect(container.querySelector('.sh-reaction-more')).toBeNull()
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

  // ── Cross-space badge ──────────────────────────────────────────────────

  it('does not render the space badge inside the space surface', () => {
    const { container } = render(
      <PostCard post={mockPost} spaceId="space-abc" surface="space" />,
    )
    expect(container.querySelector('.sh-post-space-badge')).toBeNull()
  })

  it('does not render the space badge when only an id is supplied', () => {
    // No spaceName → no cross-space context → no badge.
    const { container } = render(
      <PostCard post={mockPost} spaceId="space-abc" />,
    )
    expect(container.querySelector('.sh-post-space-badge')).toBeNull()
  })

  it('renders the space badge with the name when name + id are supplied and surface is not space', () => {
    const { container } = render(
      <PostCard post={mockPost} spaceId="space-abc" spaceName="Trip group" />,
    )
    const badge = container.querySelector(
      '.sh-post-space-badge',
    ) as HTMLAnchorElement | null
    expect(badge).toBeTruthy()
    expect(badge!.textContent).toBe('Trip group')
    expect(badge!.getAttribute('href')).toBe('/spaces/space-abc')
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

  it('renders the latest-comment preview when the field is populated', () => {
    const post: FeedPost = {
      ...mockPost,
      latest_comment: {
        id: 'c1',
        post_id: 'p1',
        parent_id: null,
        author: 'lina',
        type: 'text',
        content: 'Yes please! What time?',
        media_url: null,
        edited_at: null,
        created_at: new Date().toISOString(),
      },
    }
    const { container } = render(<PostCard post={post} />)
    const preview = container.querySelector('.sh-post-latest-comment')
    expect(preview).not.toBeNull()
    expect(preview?.textContent).toContain('Yes please!')
  })

  it('does not render the preview when latest_comment is null', () => {
    const post: FeedPost = { ...mockPost, latest_comment: null }
    const { container } = render(<PostCard post={post} />)
    expect(container.querySelector('.sh-post-latest-comment')).toBeNull()
  })

  // ── Video posts thread media_status through to VideoMedia ───────────────

  it('renders the player for a ready video post (status absent)', () => {
    const post: FeedPost = {
      ...mockPost,
      type: 'video',
      media_url: '/api/media/v.webm',
    }
    const { container } = render(<PostCard post={post} />)
    expect(container.querySelector('video')).toBeTruthy()
    expect(container.querySelector('.sh-video-processing')).toBeNull()
  })

  it('renders the processing placeholder for a still-transcoding video post', () => {
    const post: FeedPost = {
      ...mockPost,
      type: 'video',
      media_url: '/api/media/postcard-processing.webm',
      media_status: 'processing',
    }
    const { container } = render(<PostCard post={post} />)
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('.sh-video-processing')).toBeTruthy()
  })

  it('threads media_thumbnail_url to the video poster', () => {
    const post: FeedPost = {
      ...mockPost,
      type: 'video',
      media_url: '/api/media/v.webm',
      media_thumbnail_url: '/api/media/v.webp?exp=1&sig=abc',
    }
    const { container } = render(<PostCard post={post} />)
    const video = container.querySelector('video')
    expect(video).toBeTruthy()
    expect(video?.getAttribute('poster')).toBe('/api/media/v.webp?exp=1&sig=abc')
  })

  it('uses media_thumbnail_url as the processing placeholder poster', () => {
    const post: FeedPost = {
      ...mockPost,
      type: 'video',
      media_url: '/api/media/postcard-poster-processing.webm',
      media_status: 'processing',
      media_thumbnail_url: '/api/media/postcard-poster-processing.webp?exp=1&sig=xyz',
    }
    const { container } = render(<PostCard post={post} />)
    const posterImg = container.querySelector('.sh-video-processing-poster')
    expect(posterImg?.getAttribute('src')).toBe(
      '/api/media/postcard-poster-processing.webp?exp=1&sig=xyz',
    )
  })

  it('clicking the preview row triggers onComment', () => {
    const fn = vi.fn()
    const post: FeedPost = {
      ...mockPost,
      latest_comment: {
        id: 'c1',
        post_id: 'p1',
        parent_id: null,
        author: 'lina',
        type: 'text',
        content: 'Hi',
        media_url: null,
        edited_at: null,
        created_at: new Date().toISOString(),
      },
    }
    const { container } = render(<PostCard post={post} onComment={fn} />)
    const row = container.querySelector('.sh-post-latest-comment') as HTMLElement
    fireEvent.click(row)
    expect(fn).toHaveBeenCalledOnce()
  })
})

describe('PostCard link preview', () => {
  it('renders the author-built card under the text', () => {
    const post: FeedPost = {
      ...mockPost,
      content: 'read https://example.com/story',
      link_preview: {
        url: 'https://example.com/story',
        title: 'Story title',
        description: 'About it',
        site_name: 'Example',
        thumbnail_url: 'api/media/lp.webp?sig=x',
      },
    }
    const { getByText, container } = render(<PostCard post={post} />)
    expect(getByText('Story title')).toBeTruthy()
    const card = container.querySelector('.sh-link-preview a') as HTMLAnchorElement
    expect(card.href).toBe('https://example.com/story')
    // A feed card is read-only.
    expect(container.querySelector('.sh-link-preview-remove')).toBeNull()
  })

  it('renders no card without a preview', () => {
    const { container } = render(<PostCard post={mockPost} />)
    expect(container.querySelector('.sh-link-preview')).toBeNull()
  })

  describe('people open the person sheet', () => {
    it('the author name and avatar open their sheet with the cached hint', () => {
      openPersonSpy.mockReset()
      householdUsers.value = new Map([['anna', {
        user_id: 'anna', username: 'anna', display_name: 'Anna', picture_url: 'api/u/anna',
        is_admin: false, picture_hash: null, bio: null, is_new_member: false,
      }]])
      const { getByTestId, container } = render(<PostCard post={mockPost} />)
      const name = getByTestId('post-author')
      expect(name.tagName).toBe('BUTTON')
      expect(name.textContent).toBe('Anna')
      fireEvent.click(name)
      expect(openPersonSpy).toHaveBeenCalledWith('anna', {
        person: expect.objectContaining({
          display_name: 'Anna', picture_url: 'api/u/anna', is_local: true, username: 'anna',
        }),
      })
      // The avatar is the name's tappable twin — not a second tab stop.
      const face = container.querySelector('.sh-person-btn--avatar') as HTMLElement
      expect(face.getAttribute('tabindex')).toBe('-1')
      fireEvent.click(face)
      expect(openPersonSpy).toHaveBeenCalledTimes(2)
      householdUsers.value = new Map()
    })

    it('a bot post has no person to open', () => {
      const bot = { ...mockPost, author: 'system-integration' }
      const { container } = render(<PostCard post={bot} />)
      expect(container.querySelector('.sh-person-btn')).toBeNull()
    })

    it('an @mention in a space post is a button that opens that member', () => {
      openPersonSpy.mockReset()
      spaceMembers.value = {
        s1: new Map([['ru-chidi', {
          user_id: 'ru-chidi', role: 'member', joined_at: '', space_display_name: null,
          picture_hash: null, picture_url: null, display_name: 'Chidi', mention: 'chidi',
          instance_id: 'p-ok', household_name: 'Okafor family',
        }]]),
      }
      const post = { ...mockPost, content: 'thanks @chidi <img src=x onerror=alert(1)>' }
      const { container } = render(<PostCard post={post} spaceId="s1" surface="space" />)
      expect(container.querySelector('.sh-post-body img')).toBeNull()
      const mention = container.querySelector('button[data-mention="chidi"]') as HTMLButtonElement
      expect(mention).toBeTruthy()
      expect(mention.type).toBe('button')
      fireEvent.click(mention)
      expect(openPersonSpy).toHaveBeenCalledWith('ru-chidi', {
        person: expect.objectContaining({
          display_name: 'Chidi',
          household: { instance_id: 'p-ok', display_name: 'Okafor family', is_own: false },
        }),
      })
      spaceMembers.value = {}
    })
  })
})

describe('PostCard — a shared highlight', () => {
  it('shows the sharer\'s note once, not again inside the card', () => {
    const post = {
      ...mockPost, id: 'p-hl', type: 'highlight_share', content: 'Unser Sonntag 💛',
      linked_highlight_id: null,
    } as unknown as FeedPost
    const { container } = render(<PostCard post={post} />)
    const text = container.textContent ?? ''
    expect(text.split('Unser Sonntag').length - 1).toBe(1)
  })
})
