import { describe, it, expect } from 'vitest'
import { exec } from 'preact-iso/router'
import { routes } from './router'

describe('router', () => {
  it('defines 48 routes', () => {
    // Routes added / removed across recent passes:
    //   /dms/:id/calls   — per-conversation call history
    //   /calls/:callId   — in-call page
    //   /join            — space invite deep link (§23.62)
    //   /parent          — parent dashboard (§CP)
    //   /feed            — explicit household-feed route (§23 dashboard)
    //   /post/:id        — a household post's permalink (notification target)
    //   /spaces/:id/settings/:section? — space settings, one page, a URL per section
    //   /spaces/:id/about — public-style space landing (parallel to GFS)
    //   /spaces/browse   — redirects to the Spaces page's Discover
    //   /spaces/:id/zones — per-space zones admin (§23.8.7)
    //   /setup           — first-boot wizard (platform/v2)
    //   /friends         — connected-people dashboard under Browse
    //   /highlights, /highlights/new, /highlights/:highlightId — Highlights pillar (§Highlights)
    //   /highlights/archive — the Archive tab of the Highlights page (§Highlights)
    //   /momentum, /momentum/new, /momentum/:id, /momentum/:id/reply,
    //     /momentum/archive, /momentum/yours, /momentum/discover — Momentum pillar (§Momentum)
    //   /organize        — replaces standalone /tasks /shopping /stickies
    //                      (3 routes collapsed into 1 hub).
    //   /apps            — Social Home Apps browse/install page (§Apps)
    //   /apps/:appId     — sandboxed AppHost iframe launcher (§Apps)
    //   /apps/:appId/about — what an app can do; never launches it
    //   /apps/:appId/tables/:sessionId — a group game's "Who's in" page
    //   /apps/:appId/draw/:sessionId — Social Home's gift-draw result sheet
    expect(Object.keys(routes).length).toBe(49)
  })

  it('the gift draw result is a Social Home page of its own, never the app', () => {
    expect(routes['/apps/:appId/draw/:sessionId']).toBeTruthy()
    expect(routes['/apps/:appId/draw/:sessionId']).not.toBe(routes['/apps/:appId'])
  })

  it('a space and its sections are ONE route, after the fixed space sub-pages', () => {
    // /spaces/:id is the feed, /spaces/:id/chat etc. the sections — one
    // route so a section switch re-renders instead of remounting. The
    // router takes the first match, so about / settings / zones go first.
    const keys = Object.keys(routes)
    const space = keys.indexOf('/spaces/:id/:section?/:sub*')
    expect(space).toBeGreaterThan(-1)
    expect(routes['/spaces/:id']).toBeUndefined()
    for (const fixed of ['/spaces/browse', '/spaces/:id/about', '/spaces/:id/settings/:section?', '/spaces/:id/zones']) {
      expect(keys.indexOf(fixed)).toBeGreaterThan(-1)
      expect(keys.indexOf(fixed)).toBeLessThan(space)
    }
  })

  it('every album and photo has an address on ONE gallery route (no remount between them)', () => {
    const first = (url: string) => Object.keys(routes).find(r => exec(url, r))
    for (const url of ['/gallery', '/gallery/albums/a1', '/gallery/albums/a1/i1']) {
      expect(first(url)).toBe('/gallery/:sub*')
    }
    expect(exec('/gallery', '/gallery/:sub*')).toMatchObject({ params: { sub: undefined } })
    expect(exec('/gallery/albums/a1/i1', '/gallery/:sub*'))
      .toMatchObject({ params: { sub: 'albums/a1/i1' } })
  })

  it('a space gallery album and photo stay on the space route, in its Gallery section', () => {
    const first = Object.keys(routes).find(r => exec('/spaces/s1/gallery/albums/a1/i1', r))
    expect(first).toBe('/spaces/:id/:section?/:sub*')
    expect(exec('/spaces/s1/gallery/albums/a1/i1', '/spaces/:id/:section?/:sub*'))
      .toMatchObject({ params: { id: 's1', section: 'gallery', sub: 'albums/a1/i1' } })
    // The fixed sub-pages still win.
    expect(Object.keys(routes).find(r => exec('/spaces/s1/about', r))).toBe('/spaces/:id/about')
  })

  it('Yours is a Momentum address of its own, ahead of the moment page', () => {
    const keys = Object.keys(routes)
    expect(routes['/momentum/yours']).toBeTruthy()
    expect(keys.indexOf('/momentum/yours')).toBeLessThan(keys.indexOf('/momentum/:momentId'))
  })

  it('Discover is a Momentum tab ahead of the moment page; the old public addresses still resolve', () => {
    const keys = Object.keys(routes)
    expect(routes['/momentum/discover']).toBeTruthy()
    expect(keys.indexOf('/momentum/discover')).toBeLessThan(keys.indexOf('/momentum/:momentId'))
    // Old links (pairing, bookmarks) redirect: Discover → the tab,
    // sharing → Settings › Privacy.
    expect(routes['/momentum/public/discover']).toBe(routes['/momentum/discover'])
    expect(routes['/momentum/public/sharing']).toBeTruthy()
  })

  it('the settings sections are one route, so a section jump keeps the page', () => {
    expect(routes['/spaces/:id/settings/:section?']).toBeTruthy()
    expect(routes['/spaces/:id/settings']).toBeUndefined()
  })

  it('a group game has its own page; the app route does not take its address', () => {
    expect(routes['/apps/:appId/tables/:sessionId']).toBeTruthy()
    // preact-iso takes the first route that matches the whole path.
    const first = Object.keys(routes).find(r => exec('/apps/gift-draw/tables/s1', r))
    expect(first).toBe('/apps/:appId/tables/:sessionId')
    expect(exec('/apps/gift-draw/tables/s1', '/apps/:appId/tables/:sessionId'))
      .toMatchObject({ params: { appId: 'gift-draw', sessionId: 's1' } })
    expect(Object.keys(routes).find(r => exec('/apps/gift-draw', r))).toBe('/apps/:appId')
  })

  it('has feed route at /', () => {
    expect(routes['/']).toBeTruthy()
  })

  it('has all main routes', () => {
    for (const path of ['/spaces', '/dms', '/calendar', '/organize',
      '/notifications', '/pages', '/bazaar',
      '/settings', '/admin', '/connections',
      '/gallery/:sub*', '/search', '/calls', '/parent', '/momentum',
      '/corner', '/dashboard', '/apps',
      '/momentum/discover', '/momentum/public/discover', '/momentum/public/sharing']) {
      expect(routes[path]).toBeTruthy()
    }
  })
})
