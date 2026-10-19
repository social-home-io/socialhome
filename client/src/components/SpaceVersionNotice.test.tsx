import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, renderHook, waitFor, act } from '@testing-library/preact'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('@/api', () => ({ api: { get: apiGet } }))

import { SpaceVersionDetails, useSpaceCompat, type SpaceCompat } from './SpaceVersionNotice'
import { setLocale } from '@/i18n/i18n'

function compat(over: Record<string, unknown> = {}): SpaceCompat {
  return {
    ours: 18,
    min_member_proto_version: 13,
    lagging_features: ['Faster photo and video transfer', 'Admin actions from other households'],
    behind_members: [
      {
        instance_id: 'peer-13',
        display_name: "Brother's house",
        proto_version: 13,
        lacking_features: ['Faster photo and video transfer', 'Admin actions from other households'],
      },
    ],
    ...over,
  } as SpaceCompat
}

const flush = () => act(async () => { await Promise.resolve() })

describe('useSpaceCompat', () => {
  beforeEach(() => { apiGet.mockReset() })

  it('is null when no features lag', async () => {
    apiGet.mockResolvedValue(compat({ lagging_features: [], behind_members: [] }))
    const { result } = renderHook(() => useSpaceCompat('s1', true))
    await flush()
    expect(result.current).toBeNull()
  })

  it('returns the compat when a feature lags', async () => {
    apiGet.mockResolvedValue(compat())
    const { result } = renderHook(() => useSpaceCompat('s1', true))
    await waitFor(() => expect(result.current?.ours).toBe(18))
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/compat')
  })

  it('asks nothing for a viewer who may not read it', async () => {
    const { result } = renderHook(() => useSpaceCompat('s1', false))
    await flush()
    expect(apiGet).not.toHaveBeenCalled()
    expect(result.current).toBeNull()
  })

  it('is null when the request rejects (best-effort 403)', async () => {
    apiGet.mockRejectedValue(new Error('forbidden'))
    const { result } = renderHook(() => useSpaceCompat('s1', true))
    await flush()
    expect(result.current).toBeNull()
  })
})

describe('SpaceVersionDetails', () => {
  it('names the lagging features and the behind household + version', () => {
    const { container } = render(<SpaceVersionDetails compat={compat()} />)
    expect(container.textContent).toContain('Faster photo and video transfer')
    expect(container.textContent).toContain('Admin actions from other households')
    expect(container.textContent).toContain("Brother's house (v13)")
  })

  it('names the lagging features in the UI language', async () => {
    await setLocale('de')
    try {
      const { container } = render(<SpaceVersionDetails compat={compat({
        lagging_feature_keys: ['fast_media_transfer', 'remote_admin_actions'],
      })} />)
      const names = [...container.querySelectorAll('p strong')].map(el => el.textContent)
      expect(names).toEqual([
        'Schnellere Übertragung von Fotos und Videos',
        'Admin-Aktionen aus anderen Haushalten',
      ])
    } finally {
      await setLocale('en')
    }
  })
})
