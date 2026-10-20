/**
 * Interop with ``socialhome/services/highlight_public_profile.py``: a
 * profile the author's household sealed (golden vector made by
 * ``seal_profile`` with a fixed viewer key) opens in the browser.
 */
import { describe, expect, it } from 'vitest'
import {
  importViewerPrivateKey,
  makeViewerKey,
  openProfile,
} from './sealed_profile'

const PKCS8 =
  'MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgAfLj1MW2p5iAEjRWeJq83vASNFZ4mrze8BI0VniavN6hRANCAATl+yFTYtWIFIxUaNIG3kCcRISvjwFavRp6Dq/OKcrF76I74YcTYxYnuX0nqlAkVa1ezQF2yxtvhuzBgXrMR9sE'
const HEADER = {
  seal_suite: 'p256-hkdf-sha256-aes256gcm',
  epk: 'BLE1BHEgb-budwNhwxi6ZpepVqxTzWaA5XgKR5-qhOnmpnLIJvpxWgZ_Jh9r7TjDjX6irN_XiLWta-DDH0z2xcA',
  nonce: '_x571WhxmsWZ-WBZ',
}
const CT = Uint8Array.from(
  atob('AddLiGwu6r29Wn8qLSw7J3wdrBmq39Vs7PX9MEgqQF0Erd0hyHHVPNfjuxxlbPUuNZWLajED/UosBXJm+u3E4xwaKXtfRj9G5xdRBb9D79ojSQNFH7XIh4mOCjfUvRM='),
  (c) => c.charCodeAt(0),
)

describe('sealed author profile', () => {
  it('opens what the author household sealed', async () => {
    const sk = await importViewerPrivateKey(PKCS8)
    expect(await openProfile(sk, HEADER, CT, 'hl-golden')).toEqual({
      display_name: 'Anna Berger',
      bio: 'Bread & bees — Zürich.',
      picture: null,
    })
  })

  it('is bound to its highlight', async () => {
    const sk = await importViewerPrivateKey(PKCS8)
    expect(await openProfile(sk, HEADER, CT, 'another')).toBeNull()
  })

  it('refuses an unknown suite rather than guessing', async () => {
    const sk = await importViewerPrivateKey(PKCS8)
    expect(await openProfile(sk, { ...HEADER, seal_suite: 'none' }, CT, 'hl-golden')).toBeNull()
  })

  it('a fresh viewer key is a 65-byte raw point in base64url', async () => {
    const key = await makeViewerKey()
    expect(key).not.toBeNull()
    expect(key!.vk).toMatch(/^[A-Za-z0-9_-]{87}$/)
  })
})
