import { describe, it, expect, vi, beforeEach } from 'vitest'

type WsHandler = (e: { type: string; data: unknown }) => void
const wsHandlers = new Map<string, WsHandler>()

beforeEach(() => {
  vi.resetModules()
  wsHandlers.clear()
  vi.doMock('@/api', () => ({
    api: {
      get: vi.fn(async () => [
        { user_id: 'u-own', role: 'owner', mention: 'olga' },
        { user_id: 'u-adm', role: 'admin', mention: 'adam' },
        { user_id: 'u-mem', role: 'member', mention: 'mia' },
      ]),
    },
  }))
  vi.doMock('@/ws', () => ({
    ws: {
      on: vi.fn((type: string, fn: WsHandler) => {
        wsHandlers.set(type, fn)
        return () => wsHandlers.delete(type)
      }),
    },
  }))
  vi.doMock('@/store/auth', () => ({ currentUser: { value: { user_id: 'u-mem' } } }))
})

async function load() {
  const mod = await import('./spaceMembers')
  await mod.loadSpaceMembers('sp')
  return mod
}

describe('spaceMentionRender / viewerMayUseHere', () => {
  it('highlights @here only for an owner/admin author in a space that allows it', async () => {
    const m = await load()
    expect(m.spaceMentionRender('sp', 'u-own').mentions.has('here')).toBe(false)
    m.setSpaceHereAllowed('sp', true)
    expect(m.spaceMentionRender('sp', 'u-own').mentions.has('here')).toBe(true)
    expect(m.spaceMentionRender('sp', 'u-adm').mentions.has('here')).toBe(true)
    expect(m.spaceMentionRender('sp', 'u-mem').mentions.has('here')).toBe(false)
    expect(m.spaceMentionRender('sp').mentions.has('here')).toBe(false)
    // Member tokens are there either way; the viewer's own token is known.
    expect(m.spaceMentionRender('sp', 'u-mem').mentions.has('adam')).toBe(true)
    expect(m.spaceMentionRender('sp').selfMention).toBe('mia')
  })

  it('offers @here only to owners/admins of a space that allows it', async () => {
    const m = await load()
    m.setSpaceHereAllowed('sp', true)
    expect(m.viewerMayUseHere('sp')).toBe(false) // viewer is a plain member
    m.spaceMembers.value = {
      sp: new Map([['u-mem', { ...m.spaceMembers.value.sp.get('u-mem')!, role: 'admin' }]]),
    }
    expect(m.viewerMayUseHere('sp')).toBe(true)
    m.setSpaceHereAllowed('sp', false)
    expect(m.viewerMayUseHere('sp')).toBe(false)
    expect(m.viewerMayUseHere(null)).toBe(false)
  })

  it('is empty until the roster loads', async () => {
    const m = await import('./spaceMembers')
    expect(m.spaceMentionRender('sp', 'u-own').mentions.size).toBe(0)
  })
})

describe('space.member.profile_updated frame', () => {
  async function loadWithFrames() {
    const m = await load()
    const frame = (data: Record<string, unknown>) =>
      wsHandlers.get('space.member.profile_updated')!({
        type: 'space.member.profile_updated',
        data: { type: 'space.member.profile_updated', ...data },
      })
    return { m, frame }
  }

  it('takes a picture a space catch-up landed (same frame as a live change)', async () => {
    const { m, frame } = await loadWithFrames()
    frame({
      space_id: 'sp', user_id: 'u-adm', space_display_name: 'Adam',
      picture_hash: '0123456789abcdef',
      picture_url: '/api/spaces/sp/members/u-adm/picture?v=0123456789abcdef',
    })
    const row = m.spaceMembers.value.sp.get('u-adm')!
    expect(row.picture_hash).toBe('0123456789abcdef')
    expect(row.picture_url).toBe('/api/spaces/sp/members/u-adm/picture?v=0123456789abcdef')
    expect(row.space_display_name).toBe('Adam')
    expect(row.role).toBe('admin')
  })

  it('drops the picture on a caught-up reset (null hash → initials)', async () => {
    const { m, frame } = await loadWithFrames()
    frame({
      space_id: 'sp', user_id: 'u-adm', space_display_name: null,
      picture_hash: 'fedcba9876543210', picture_url: '/api/spaces/sp/members/u-adm/picture?v=fedcba9876543210',
    })
    frame({
      space_id: 'sp', user_id: 'u-adm', space_display_name: null,
      picture_hash: null, picture_url: null,
    })
    const row = m.spaceMembers.value.sp.get('u-adm')!
    expect(row.picture_hash).toBeNull()
    expect(row.picture_url).toBeNull()
    const { resolveAvatar } = await import('@/utils/avatar')
    expect(resolveAvatar('sp', 'u-adm')).toBeNull()
  })

  it('ignores a frame for a member not in the loaded roster', async () => {
    const { m, frame } = await loadWithFrames()
    const before = m.spaceMembers.value
    frame({ space_id: 'sp', user_id: 'u-gone', space_display_name: null, picture_hash: null, picture_url: null })
    expect(m.spaceMembers.value).toBe(before)
  })
})
