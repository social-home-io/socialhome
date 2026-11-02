import { describe, it, expect } from 'vitest'
import { albumDisplayName } from './albumName'

describe('albumDisplayName', () => {
  it('names the auto Posts album and the unrenamed default album in the reader\'s language', () => {
    expect(albumDisplayName({ name: 'Posts from the server', is_system: true })).toBe('Posts')
    // Made by a German uploader as "Fotos": an English reader sees "Photos".
    expect(albumDisplayName({ name: 'Fotos', default_name: true })).toBe('Photos')
  })

  it('keeps every other name as people typed it', () => {
    expect(albumDisplayName({ name: 'Familie' })).toBe('Familie')
    expect(albumDisplayName({ name: 'Familie', default_name: false })).toBe('Familie')
  })
})
