import { describe, it, expect } from 'vitest'
import { formatBytes } from './bytes'

describe('formatBytes', () => {
  it('names the unit people read, in binary steps', () => {
    expect(formatBytes(0)).toBe('0 byte')
    expect(formatBytes(1536)).toBe('2 kB')
    expect(formatBytes(350 * 1024 ** 2)).toBe('350 MB')
    expect(formatBytes(2 * 1024 ** 3)).toBe('2 GB')
    expect(formatBytes(1.25 * 1024 ** 3)).toBe('1.3 GB')
  })

  it('never shows a negative size', () => {
    expect(formatBytes(-5)).toBe('0 byte')
  })
})
