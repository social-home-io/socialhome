import { afterEach, describe, expect, it } from 'vitest'
import { setLocale } from '@/i18n/i18n'
import { coordLabel } from './coordLabel'

describe('coordLabel', () => {
  afterEach(async () => { await setLocale('en') })

  it('names the hemisphere and keeps 4 dp', () => {
    expect(coordLabel(47.3769, 8.5417)).toBe('47.3769° N, 8.5417° E')
    expect(coordLabel(-33.8688, -70.1)).toBe('33.8688° S, 70.1000° W')
  })

  it('follows the UI locale', async () => {
    await setLocale('de')
    expect(coordLabel(47.3769, 8.5417)).toMatch(/^47,3769° N, 8,5417° (E|O)$/)
  })
})
