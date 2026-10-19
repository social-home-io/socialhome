/**
 * The retired ``/spaces/browse`` lands on the Spaces page's Discover —
 * with and without the HA ingress prefix, replacing the history entry.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/preact'

const START = location.pathname

const PREFIX = '/api/hassio_ingress/tok/'
const ingressBase = () => ({
  basePath: PREFIX,
  addBase: (p: string) => {
    const [pathOnly, ...rest] = p.split(/(?=[?#])/, 2)
    const normalised = pathOnly.startsWith('/') ? pathOnly : '/' + pathOnly
    return PREFIX.replace(/\/+$/, '') + normalised + rest.join('')
  },
  stripBase: (p: string) => {
    const prefix = PREFIX.replace(/\/+$/, '')
    if (p === prefix) return '/'
    if (p.startsWith(prefix + '/')) return p.slice(prefix.length) || '/'
    return p
  },
})

async function mount() {
  const { IngressLocationProvider } = await import('@/router/IngressLocationProvider')
  const { default: Redirect } = await import('./SpaceBrowseRedirect')
  render(<IngressLocationProvider><Redirect /></IngressLocationProvider>)
}

beforeEach(() => { vi.resetModules() })
afterEach(() => {
  cleanup()
  vi.doUnmock('@/baseUrl')
  vi.resetModules()
  history.replaceState(null, '', START)
})

describe('SpaceBrowseRedirect', () => {
  it('replaces /spaces/browse with /spaces?discover=household', async () => {
    history.replaceState(null, '', '/spaces/browse')
    const replace = vi.spyOn(history, 'replaceState')
    await mount()
    expect(location.pathname + location.search).toBe('/spaces?discover=household')
    expect(replace).toHaveBeenCalledWith(null, '', '/spaces?discover=household')
    replace.mockRestore()
  })

  it('keeps the old tab: ?tab=friends → Paired households', async () => {
    history.replaceState(null, '', '/spaces/browse?tab=friends')
    await mount()
    expect(location.search).toBe('?discover=paired')
  })

  it('keeps the HA ingress prefix on the address bar', async () => {
    vi.doMock('@/baseUrl', ingressBase)
    history.replaceState(null, '', '/api/hassio_ingress/tok/spaces/browse?discover=global')
    await mount()
    expect(location.pathname + location.search).toBe('/api/hassio_ingress/tok/spaces?discover=global')
  })
})
