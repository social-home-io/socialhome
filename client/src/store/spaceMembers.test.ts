import { describe, it, expect, vi, beforeEach } from 'vitest'

beforeEach(() => {
  vi.resetModules()
  vi.doMock('@/api', () => ({
    api: {
      get: vi.fn(async () => [
        { user_id: 'u-own', role: 'owner', mention: 'olga' },
        { user_id: 'u-adm', role: 'admin', mention: 'adam' },
        { user_id: 'u-mem', role: 'member', mention: 'mia' },
      ]),
    },
  }))
  vi.doMock('@/ws', () => ({ ws: { on: vi.fn() } }))
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
