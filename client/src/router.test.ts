import { describe, it, expect } from 'vitest'
import { routes } from './router'

describe('router', () => {
  it('defines 42 routes', () => {
    // Routes added / removed across recent passes:
    //   /dms/:id/calls   — per-conversation call history
    //   /calls/:callId   — in-call page
    //   /join            — space invite deep link (§23.62)
    //   /parent          — parent dashboard (§CP)
    //   /feed            — explicit household-feed route (§23 dashboard)
    //   /spaces/:id/settings — admin space settings (§23 customization)
    //   /spaces/:id/about — public-style space landing (parallel to GFS)
    //   /spaces/browse   — unified space browser (§D3)
    //   /spaces/:id/zones — per-space zones admin (§23.8.7)
    //   /setup           — first-boot wizard (platform/v2)
    //   /friends         — connected-people dashboard under Browse
    //   /highlights, /highlights/new, /highlights/:highlightId — Highlights pillar (§Highlights)
    //   /highlights/archive — month-grid history browser (§Highlights)
    //   /momentum, /momentum/new, /momentum/:id, /momentum/:id/reply,
    //     /momentum/archive — Momentum pillar (§Momentum)
    //   /organize        — replaces standalone /tasks /shopping /stickies
    //                      (3 routes collapsed into 1 hub).
    //   /apps            — Social Home Apps browse/install page (§Apps)
    //   /apps/:appId     — sandboxed AppHost iframe launcher (§Apps)
    expect(Object.keys(routes).length).toBe(43)
  })

  it('a space and its sections are ONE route, after the fixed space sub-pages', () => {
    // /spaces/:id is the feed, /spaces/:id/chat etc. the sections — one
    // route so a section switch re-renders instead of remounting. The
    // router takes the first match, so about / settings / zones go first.
    const keys = Object.keys(routes)
    const space = keys.indexOf('/spaces/:id/:section?')
    expect(space).toBeGreaterThan(-1)
    expect(routes['/spaces/:id']).toBeUndefined()
    for (const fixed of ['/spaces/browse', '/spaces/:id/about', '/spaces/:id/settings', '/spaces/:id/zones']) {
      expect(keys.indexOf(fixed)).toBeGreaterThan(-1)
      expect(keys.indexOf(fixed)).toBeLessThan(space)
    }
  })

  it('has feed route at /', () => {
    expect(routes['/']).toBeTruthy()
  })

  it('has all main routes', () => {
    for (const path of ['/spaces', '/dms', '/calendar', '/organize',
      '/notifications', '/pages', '/bazaar',
      '/settings', '/admin', '/connections',
      '/gallery', '/search', '/calls', '/parent', '/momentum',
      '/corner', '/dashboard', '/apps',
      '/momentum/public/discover', '/momentum/public/sharing']) {
      expect(routes[path]).toBeTruthy()
    }
  })
})
