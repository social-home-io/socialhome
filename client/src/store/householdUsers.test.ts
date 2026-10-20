import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api', () => ({ api: { get: vi.fn() } }))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))

import type { User } from '@/types'
import {
  householdUsers,
  namedPersonLabel,
  namedPersonPicture,
} from './householdUsers'

describe('namedPersonLabel / namedPersonPicture', () => {
  beforeEach(() => {
    householdUsers.value = new Map([
      ['uid-bob', {
        user_id: 'uid-bob', username: 'bob', display_name: 'Bob',
        picture_url: 'api/users/uid-bob/picture?v=1&sig=x',
      } as User],
    ])
  })

  it('names a friend from another household the cache does not know', () => {
    const amaka = {
      display_name: 'Amaka Okafor',
      picture_url: 'api/users/ru-amaka/picture?v=2&sig=y',
    }
    expect(namedPersonLabel('ru-amaka', amaka)).toBe('Amaka Okafor')
    expect(namedPersonPicture('ru-amaka', amaka)).toBe(
      'api/users/ru-amaka/picture?v=2&sig=y',
    )
  })

  it('an id the server could not name reads "Someone", never the id', () => {
    const nobody = { display_name: null, picture_url: null }
    expect(namedPersonLabel('ru-forgotten', nobody)).toBe('Someone')
    expect(namedPersonPicture('ru-forgotten', nobody)).toBeNull()
  })

  it('a picture the server withheld stays withheld (guardian block)', () => {
    // The cache has Bob's picture — a null from the server is final.
    const nameOnly = { display_name: 'Bob', picture_url: null }
    expect(namedPersonPicture('uid-bob', nameOnly)).toBeNull()
  })

  it('falls back to the household cache for a payload without the fields', () => {
    expect(namedPersonLabel('uid-bob', undefined)).toBe('Bob')
    expect(namedPersonLabel('uid-bob', {})).toBe('Bob')
    expect(namedPersonPicture('uid-bob', {})).toBe(
      'api/users/uid-bob/picture?v=1&sig=x',
    )
    expect(namedPersonLabel('ru-amaka', undefined)).toBe('Someone')
  })

  it('no user id → empty label, no picture', () => {
    expect(namedPersonLabel(null, { display_name: 'X' })).toBe('')
    expect(namedPersonPicture(undefined, { picture_url: 'api/x' })).toBeNull()
  })
})
