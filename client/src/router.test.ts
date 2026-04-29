import { describe, it, expect } from 'vitest'
import { routes } from './router'

describe('router', () => {
  it('defines 29 routes', () => {
    // Routes added across recent passes:
    //   /dms/:id/calls   — per-conversation call history
    //   /calls/:callId   — in-call page
    //   /join            — space invite deep link (§23.62)
    //   /parent          — parent dashboard (§CP)
    //   /feed            — explicit household-feed route (§23 dashboard)
    //   /spaces/:id/settings — admin space settings (§23 customization)
    //   /spaces/browse   — unified space browser (§D3)
    //   /spaces/:id/zones — per-space zones admin (§23.8.7)
    //   /setup           — first-boot wizard (platform/v2)
    expect(Object.keys(routes).length).toBe(29)
  })

  it('has feed route at /', () => {
    expect(routes['/']).toBeTruthy()
  })

  it('has all main routes', () => {
    for (const path of ['/spaces', '/dms', '/calendar', '/shopping',
      '/notifications', '/tasks', '/pages', '/stickies', '/bazaar',
      '/settings', '/admin', '/connections',
      '/gallery', '/search', '/calls', '/parent']) {
      expect(routes[path]).toBeTruthy()
    }
  })
})
