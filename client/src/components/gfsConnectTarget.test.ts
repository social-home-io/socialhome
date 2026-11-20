import { describe, it, expect } from 'vitest'
import { gfsConnectTarget } from './gfsConnectTarget'

describe('gfsConnectTarget', () => {
  it('keeps a plain http(s) GFS address, without a trailing slash', () => {
    expect(gfsConnectTarget('https://club.example/')).toBe('https://club.example')
    expect(gfsConnectTarget('https://club.example/gfs')).toBe('https://club.example/gfs')
    expect(gfsConnectTarget('http://192.168.1.5:8765')).toBe('http://192.168.1.5:8765')
  })

  it('refuses anything else', () => {
    for (const bad of [
      null, undefined, '', 'club.example', 'javascript:alert(1)', 'data:text/html,x',
      'socialhome://gfs-pair/https://club.example?token=t', 'https://u:p@club.example',
      'https://club.example/?token=t', 'https://club.example/#x',
    ]) {
      expect(gfsConnectTarget(bad)).toBeNull()
    }
  })
})
