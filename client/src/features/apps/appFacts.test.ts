import { describe, it, expect } from 'vitest'
import {
  languageNames,
  neverList,
  permissionDetail,
  permissionTitle,
  permissionsOf,
  ratingLabel,
  sizeLabel,
  sourceLabel,
  versionLabel,
} from './appFacts'

describe('appFacts', () => {
  it('permission text is Social Home\'s own words', () => {
    expect(permissionTitle('federation')).toBe('Sends invites and moves to other households')
    expect(permissionDetail('storage', 'Chess')).toContain('Only Chess can read it')
    expect(neverList()).toHaveLength(4)
  })

  it('a CalVer version reads as a date, anything else as v…', () => {
    expect(versionLabel('2026.10.29')).toMatch(/29.*2026|2026.*29/)
    expect(versionLabel('2026.10.29.1')).toMatch(/2026/)
    expect(versionLabel('1.2.3')).toBe('v1.2.3')
  })

  it('sizes, ratings and the source label', () => {
    expect(sizeLabel(16198)).toMatch(/16\s?kB|16\s?KB/i)
    expect(sizeLabel(3 * 1024 * 1024)).toMatch(/3\s?MB/)
    expect(sizeLabel(null)).toBe('')
    expect(ratingLabel(0)).toBe('Everyone')
    expect(ratingLabel(13)).toBe('13+')
    expect(sourceLabel(null)).toBe('From Social Home')
    expect(sourceLabel('apps.example.org')).toBe('From apps.example.org, set by your server operator')
  })

  it('languages by their own names', () => {
    expect(languageNames(['en', 'de'])).toEqual(['English', 'Deutsch'])
    expect(languageNames(undefined)).toEqual([])
  })

  it('permissions: the agreed ones once installed, people implied for old data', () => {
    expect(permissionsOf({ permissions: ['storage'] } as any, { permissions: ['federation'] } as any)).toEqual(['storage'])
    expect(permissionsOf(null, { permissions: ['people'] } as any)).toEqual(['people'])
    expect(permissionsOf(null, { capabilities: ['federation'] } as any)).toEqual(['people', 'federation'])
  })
})
