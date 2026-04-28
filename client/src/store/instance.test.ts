import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { instanceConfig, instanceConfigError, loadInstanceConfig, hasCapability } from './instance'

describe('instance store', () => {
  let fetchSpy: any

  beforeEach(() => {
    instanceConfig.value = null
    instanceConfigError.value = null
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('loadInstanceConfig populates the signal', async () => {
    fetchSpy.mockResolvedValue({
      status: 200, ok: true,
      json: async () => ({
        mode: 'standalone',
        instance_name: 'Home',
        capabilities: ['password_auth', 'push'],
        setup_required: true,
      }),
    } as any)
    const cfg = await loadInstanceConfig()
    expect(cfg.mode).toBe('standalone')
    expect(instanceConfig.value?.setup_required).toBe(true)
    expect(instanceConfigError.value).toBeNull()
  })

  it('hasCapability checks the loaded config', async () => {
    fetchSpy.mockResolvedValue({
      status: 200, ok: true,
      json: async () => ({
        mode: 'haos',
        instance_name: 'Home',
        capabilities: ['ingress', 'push', 'ai'],
        setup_required: false,
      }),
    } as any)
    await loadInstanceConfig()
    expect(hasCapability('ingress')).toBe(true)
    expect(hasCapability('stt')).toBe(false)
  })

  it('records error when fetch fails', async () => {
    fetchSpy.mockResolvedValue({
      status: 500, ok: false, json: async () => ({}),
    } as any)
    await expect(loadInstanceConfig()).rejects.toThrow()
    expect(instanceConfigError.value).toBeTruthy()
  })
})
