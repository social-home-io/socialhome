import { describe, it, expect, afterEach } from 'vitest'
import { instanceConfig } from '@/store/instance'
import {
  platformMode,
  isHomeAssistant,
  isSupervisorAddon,
  usesIngressAuth,
  managesLocalUsers,
  requiresHaUserPassword,
  usesHaUserDirectory,
  supportsStt,
  supportsAi,
  supportsPush,
  supportsZoneSync,
  servedThroughIngress,
} from './index'

function set(mode: 'standalone' | 'ha' | 'haos' | null, caps: string[] = []) {
  instanceConfig.value = mode === null ? null : {
    mode,
    instance_name: 'Home',
    instance_id: 'i1',
    capabilities: caps,
    setup_required: false,
  }
}

afterEach(() => { instanceConfig.value = null })

describe('platform adapter', () => {
  it('platformMode reflects the loaded config (null before load)', () => {
    set(null)
    expect(platformMode()).toBeNull()
    set('haos')
    expect(platformMode()).toBe('haos')
  })

  it('isHomeAssistant is true for ha and haos only', () => {
    set('standalone'); expect(isHomeAssistant()).toBe(false)
    set('ha'); expect(isHomeAssistant()).toBe(true)
    set('haos'); expect(isHomeAssistant()).toBe(true)
  })

  it('isSupervisorAddon / usesIngressAuth are haos only', () => {
    set('ha'); expect(isSupervisorAddon()).toBe(false); expect(usesIngressAuth()).toBe(false)
    set('haos'); expect(isSupervisorAddon()).toBe(true); expect(usesIngressAuth()).toBe(true)
  })

  it('managesLocalUsers is standalone only', () => {
    set('standalone'); expect(managesLocalUsers()).toBe(true)
    set('ha'); expect(managesLocalUsers()).toBe(false)
    set('haos'); expect(managesLocalUsers()).toBe(false)
  })

  it('requiresHaUserPassword is ha only (haos signs in via ingress)', () => {
    set('ha'); expect(requiresHaUserPassword()).toBe(true)
    set('haos'); expect(requiresHaUserPassword()).toBe(false)
    set('standalone'); expect(requiresHaUserPassword()).toBe(false)
  })

  it('capability accessors read the capabilities array', () => {
    set('ha', ['stt', 'ai', 'push', 'ha_person_directory', 'zone_sync'])
    expect(supportsZoneSync()).toBe(true)
    expect(supportsStt()).toBe(true)
    expect(supportsAi()).toBe(true)
    expect(supportsPush()).toBe(true)
    expect(usesHaUserDirectory()).toBe(true)
    set('standalone', [])
    expect(supportsStt()).toBe(false)
    expect(usesHaUserDirectory()).toBe(false)
    expect(supportsZoneSync()).toBe(false)
  })

  it('servedThroughIngress reads the ingress capability', () => {
    set('haos', ['ingress']); expect(servedThroughIngress()).toBe(true)
    set('ha', []); expect(servedThroughIngress()).toBe(false)
  })

  it('all accessors are safe before config loads', () => {
    set(null)
    expect(isHomeAssistant()).toBe(false)
    expect(supportsStt()).toBe(false)
    expect(managesLocalUsers()).toBe(false)
    expect(servedThroughIngress()).toBe(false)
  })
})
