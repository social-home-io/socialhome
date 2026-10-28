/**
 * The Home Assistant App panel's route intake.
 *
 * HA's ``ha-panel-app`` (home-assistant/frontend @ 53dc14f4) always loads
 * the ingress root in its iframe and posts the route after ``/<slug>`` as
 * ``home-assistant/properties`` once the iframe subscribes. These tests
 * play HA's side of that conversation: a fake parent window whose
 * ``postMessage`` records the subscribe, and ``MessageEvent``s whose
 * ``source`` is that parent.
 */
import { render, cleanup } from '@testing-library/preact'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import {
  HA_PANEL_ROUTE_PREFIXES,
  HaPanelRoute,
  parseHaPanelTarget,
  startHaPanelRoute,
} from './haPanelRoute'
import { IngressLocationProvider } from './IngressLocationProvider'
import { instanceConfig } from '@/store/instance'

const PREFIXES = new Set(['', 'calendar', 'dms', 'settings', 'spaces', 'post'])

describe('parseHaPanelTarget', () => {
  it('takes a plain in-app path', () => {
    expect(parseHaPanelTarget('/dms/c1', PREFIXES)).toBe('/dms/c1')
    expect(parseHaPanelTarget('/spaces/s1/chat', PREFIXES)).toBe('/spaces/s1/chat')
  })

  it('decodes the query and hash the server folded into the path', () => {
    expect(parseHaPanelTarget('/calendar%3Fevent%3De1', PREFIXES)).toBe('/calendar?event=e1')
    expect(parseHaPanelTarget('/settings%23privacy', PREFIXES)).toBe('/settings#privacy')
  })

  it('decodes what the Companion apps re-encode (only ?/# escaped)', () => {
    // Android UrlUtil.buildRelativeUrl / iOS webviewURL(from:) decode the
    // path and let the URL builder re-escape only what a segment can't hold.
    expect(parseHaPanelTarget('/calendar%3Fevent=e1', PREFIXES)).toBe('/calendar?event=e1')
  })

  it('answers null for nothing to open', () => {
    expect(parseHaPanelTarget('', PREFIXES)).toBeNull()
  })

  it('takes the root', () => {
    expect(parseHaPanelTarget('/', PREFIXES)).toBe('/')
  })

  it.each([
    ['protocol-relative', '//evil.example/x'],
    ['encoded protocol-relative', '/%2Fevil.example/x'],
    ['backslash', '/\\evil.example'],
    ['encoded backslash', '/%5Cevil.example'],
    ['absolute URL', 'https://evil.example/'],
    ['encoded absolute URL', 'https%3A%2F%2Fevil.example'],
    ['javascript:', 'javascript:alert(1)'],
    ['no leading slash', 'dms/c1'],
    ['unknown route', '/api/admin/users'],
    ['control character', '/dms/%0Aevil'],
    ['malformed escape', '/dms/%E0%A4%A'],
    ['dot segments out of a known route', '/dms/../api/me'],
  ])('rejects %s', (_label, raw) => {
    expect(parseHaPanelTarget(raw, PREFIXES)).toBeNull()
  })

  it('the allow-list is the SPA route table', () => {
    for (const p of ['', 'feed', 'dms', 'calendar', 'settings', 'spaces', 'post', 'notifications']) {
      expect(HA_PANEL_ROUTE_PREFIXES.has(p)).toBe(true)
    }
    expect(HA_PANEL_ROUTE_PREFIXES.has('api')).toBe(false)
  })
})

/** HA's side: a parent window that records what the iframe posts. */
function fakeParent() {
  const posted: Array<{ data: unknown; origin: string }> = []
  const parent = {
    postMessage: (data: unknown, origin: string) => posted.push({ data, origin }),
  } as unknown as Window
  return { parent, posted }
}

function fromHa(parent: Window, route: unknown, origin = location.origin) {
  window.dispatchEvent(new MessageEvent('message', {
    data: { type: 'home-assistant/properties', narrow: false, route },
    origin,
    source: parent as unknown as MessageEventSource,
  }))
}

describe('startHaPanelRoute', () => {
  let navigate: ReturnType<typeof vi.fn<(target: string, replace: boolean) => void>>

  beforeEach(() => { navigate = vi.fn() })

  it('subscribes to the panel properties on the same origin only', () => {
    const { parent, posted } = fakeParent()
    const stop = startHaPanelRoute(navigate, { parent, prefixes: PREFIXES })
    expect(posted).toEqual([
      { data: { type: 'home-assistant/subscribe-properties' }, origin: location.origin },
    ])
    stop()
    expect(posted[1]).toEqual({
      data: { type: 'home-assistant/unsubscribe-properties' }, origin: location.origin,
    })
  })

  it('does nothing outside a frame', () => {
    const stop = startHaPanelRoute(navigate, { parent: window, prefixes: PREFIXES })
    fromHa(window, { prefix: '/socialhome', path: '/dms/c1' })
    expect(navigate).not.toHaveBeenCalled()
    stop()
  })

  it('opens the route tail on the first message, replacing the root', () => {
    const { parent } = fakeParent()
    const stop = startHaPanelRoute(navigate, { parent, prefixes: PREFIXES })
    fromHa(parent, { prefix: '/a0d7b954_socialhome', path: '/calendar%3Fevent%3De1' })
    expect(navigate).toHaveBeenCalledWith('/calendar?event=e1', true)
    stop()
  })

  it('a bare panel route opens nothing', () => {
    const { parent } = fakeParent()
    const stop = startHaPanelRoute(navigate, { parent, prefixes: PREFIXES })
    fromHa(parent, { prefix: '/a0d7b954_socialhome', path: '' })
    expect(navigate).not.toHaveBeenCalled()
    stop()
  })

  it('re-sent properties (resize, narrow) never pull the user back', () => {
    const { parent } = fakeParent()
    const stop = startHaPanelRoute(navigate, { parent, prefixes: PREFIXES })
    fromHa(parent, { prefix: '/sh', path: '/dms/c1' })
    fromHa(parent, { prefix: '/sh', path: '/dms/c1' })
    expect(navigate).toHaveBeenCalledTimes(1)
    stop()
  })

  it('a later route change (a tap while the panel is open) pushes', () => {
    const { parent } = fakeParent()
    const stop = startHaPanelRoute(navigate, { parent, prefixes: PREFIXES })
    fromHa(parent, { prefix: '/sh', path: '' })
    fromHa(parent, { prefix: '/sh', path: '/post/p1' })
    expect(navigate).toHaveBeenCalledWith('/post/p1', false)
    stop()
  })

  it('ignores messages from anyone but the parent, another origin or another type', () => {
    const { parent } = fakeParent()
    const other = fakeParent().parent
    const stop = startHaPanelRoute(navigate, { parent, prefixes: PREFIXES })
    fromHa(other, { prefix: '/sh', path: '/dms/c1' })
    fromHa(parent, { prefix: '/sh', path: '/dms/c1' }, 'https://evil.example')
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'something-else', route: { prefix: '/sh', path: '/dms/c1' } },
      origin: location.origin,
      source: parent as unknown as MessageEventSource,
    }))
    fromHa(parent, 'not-a-route')
    fromHa(parent, { prefix: '/sh', path: 42 })
    expect(navigate).not.toHaveBeenCalled()
    stop()
  })

  it('ignores a route tail that is not an in-app path', () => {
    const { parent } = fakeParent()
    const stop = startHaPanelRoute(navigate, { parent, prefixes: PREFIXES })
    fromHa(parent, { prefix: '/sh', path: '//evil.example/x' })
    expect(navigate).not.toHaveBeenCalled()
    stop()
  })

  it('stops listening once stopped', () => {
    const { parent } = fakeParent()
    startHaPanelRoute(navigate, { parent, prefixes: PREFIXES })()
    fromHa(parent, { prefix: '/sh', path: '/dms/c1' })
    expect(navigate).not.toHaveBeenCalled()
  })
})

describe('<HaPanelRoute>', () => {
  afterEach(() => {
    cleanup()
    instanceConfig.value = null
    history.replaceState(null, '', '/')
  })

  function setIngress(on: boolean) {
    instanceConfig.value = {
      mode: on ? 'haos' : 'standalone',
      instance_name: 'Home',
      instance_id: 'i1',
      capabilities: on ? ['ingress'] : [],
      setup_required: false,
    }
  }

  it('routes the app to the tail HA posts when served through ingress', () => {
    setIngress(true)
    const { parent } = fakeParent()
    history.replaceState(null, '', '/')
    render(
      <IngressLocationProvider>
        <HaPanelRoute parent={parent} />
      </IngressLocationProvider>,
    )
    fromHa(parent, { prefix: '/sh', path: '/dms/c1' })
    expect(location.pathname).toBe('/dms/c1')
  })

  it('stays out of the way without the ingress capability', () => {
    setIngress(false)
    const { parent, posted } = fakeParent()
    render(
      <IngressLocationProvider>
        <HaPanelRoute parent={parent} />
      </IngressLocationProvider>,
    )
    fromHa(parent, { prefix: '/sh', path: '/dms/c1' })
    expect(posted).toEqual([])
    expect(location.pathname).toBe('/')
  })
})
