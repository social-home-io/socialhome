import { describe, it, expect } from 'vitest'
import { describeUploadError, UploadHttpError, uploadHttpError } from './uploadErrors'

const file = (name: string, sizeMb: number): File => {
  const blob = new Blob([new Uint8Array(sizeMb * 1024 * 1024)])
  return new File([blob], name, { type: 'image/jpeg' })
}

describe('describeUploadError', () => {
  it('detects 413 too-large', () => {
    const msg = describeUploadError(
      new Error('Upload failed (413): too big'),
      { file: file('cat.jpg', 32) },
    )
    expect(msg).toMatch(/too large/i)
    expect(msg).toContain('cat.jpg')
    expect(msg).toContain('32 MB')
  })

  it('detects 415 unsupported type', () => {
    const msg = describeUploadError(new Error('API 415: /api/media/upload'))
    expect(msg).toMatch(/file type isn't supported/i)
  })

  it('detects 429 throttle', () => {
    const msg = describeUploadError(new Error('API 429: /api/media/upload'))
    expect(msg).toMatch(/uploaded a lot recently/i)
  })

  it('detects network failure', () => {
    const msg = describeUploadError(new TypeError('Failed to fetch'))
    expect(msg).toMatch(/couldn't reach the server/i)
  })

  it('falls back to a trimmed raw message when nothing matches', () => {
    const msg = describeUploadError(new Error('disk on fire'))
    expect(msg).toContain('disk on fire')
  })

  it('handles non-Error values', () => {
    const msg = describeUploadError('boom')
    expect(msg).toContain('boom')
  })

  it('uses the file name in the lead when provided', () => {
    const msg = describeUploadError(new Error('boom'), {
      file: file('trip.mp4', 1),
    })
    expect(msg.startsWith("Couldn't upload trip.mp4")).toBe(true)
  })
})

describe('describeUploadError with an UploadHttpError (gallery live walk)', () => {
  const body = (code: string) =>
    JSON.stringify({ error: { code, detail: "This image couldn't be opened." } })

  it('a PDF refused as IMAGE_UNREADABLE reads as an unsupported type, never raw JSON', () => {
    const msg = describeUploadError(uploadHttpError(422, body('IMAGE_UNREADABLE')), {
      file: file('list.pdf', 1),
    })
    expect(msg).toMatch(/file type isn't supported/i)
    expect(msg).not.toContain('{')
    expect(msg).not.toContain('IMAGE_UNREADABLE')
  })

  it('a full household storage has its own message', () => {
    const msg = describeUploadError(uploadHttpError(507, body('STORAGE_FULL')))
    expect(msg).toMatch(/storage is full/i)
  })

  it('the client size guard is the translated too-large message', () => {
    const msg = describeUploadError(new UploadHttpError(413, 'PAYLOAD_TOO_LARGE'), {
      file: file('huge.jpg', 2),
    })
    expect(msg).toMatch(/too large/i)
    expect(msg).toContain('2 MB')
  })

  it('an unknown code never shows its body', () => {
    const msg = describeUploadError(uploadHttpError(409, '{"error": {"code": "X"}}'))
    expect(msg).not.toContain('{')
    expect(msg).toMatch(/server hit an error/i)
  })

  it('a non-JSON body still parses to a status', () => {
    const err = uploadHttpError(429, 'Too many')
    expect(err.code).toBeNull()
    expect(describeUploadError(err)).toMatch(/uploaded a lot recently/i)
  })
})
