import { describe, it, expect } from 'vitest'
import {
  buildInviteCode, decodeInviteCode, gfsBaseFromInviteUrl, inviteLinkUrl,
} from './spaceInviteCode'

describe('spaceInviteCode', () => {
  describe('build + decode round-trip', () => {
    it('preserves all fields through encode → decode', () => {
      const payload = {
        token: 'a1b2c3d4e5f60718',
        space_id: 'space-uuid-here',
        space_display_hint: "Pascal's family · 🏠",
        issuer_instance_id: 'abcdef1234567890abcdef1234567890',
        via_gfs: {
          gfs_url: 'https://gfs.example.com/',
          gfs_space_id: 'gfs-space-id-here',
        },
      }
      const code = buildInviteCode(payload)
      expect(code).toMatch(/^socialhome:\/\/invite#/)
      expect(decodeInviteCode(code)).toEqual(payload)
    })

    it('round-trips a minimal payload (just token)', () => {
      const code = buildInviteCode({ token: 'a1b2c3d4e5f60718' })
      expect(decodeInviteCode(code)).toEqual({ token: 'a1b2c3d4e5f60718' })
    })
  })

  describe('back-compat decode shapes', () => {
    it('accepts a raw JSON payload', () => {
      const json = JSON.stringify({
        token: 'a1b2c3d4e5f60718',
        space_id: 'space-uuid',
      })
      expect(decodeInviteCode(json)).toEqual({
        token: 'a1b2c3d4e5f60718',
        space_id: 'space-uuid',
      })
    })

    it('accepts a bare hex token (back-compat with old share-dialog copies)', () => {
      const out = decodeInviteCode('a1b2c3d4e5f60718')
      expect(out).toEqual({ token: 'a1b2c3d4e5f60718' })
    })

    it('trims surrounding whitespace before decoding', () => {
      const out = decodeInviteCode('   a1b2c3d4e5f60718  \n')
      expect(out?.token).toBe('a1b2c3d4e5f60718')
    })
  })

  describe('garbage rejection', () => {
    it('returns null for empty input', () => {
      expect(decodeInviteCode('')).toBeNull()
      expect(decodeInviteCode('   ')).toBeNull()
    })

    it('returns null for socialhome://pair (wrong scheme path)', () => {
      // Pairing codes must not be silently accepted as invites.
      expect(decodeInviteCode('socialhome://pair#abc')).toBeNull()
    })

    it('returns null for socialhome://invite# with non-base64 garbage', () => {
      expect(decodeInviteCode('socialhome://invite#!@#$%')).toBeNull()
    })

    it('returns null for socialhome://invite# with valid base64 of non-JSON', () => {
      // base64url("hello") = "aGVsbG8"
      expect(decodeInviteCode('socialhome://invite#aGVsbG8')).toBeNull()
    })

    it('returns null when the decoded JSON is missing token', () => {
      // base64url('{"space_id":"x"}') = "eyJzcGFjZV9pZCI6IngifQ"
      expect(
        decodeInviteCode('socialhome://invite#eyJzcGFjZV9pZCI6IngifQ'),
      ).toBeNull()
    })

    it('returns null for a non-hex bare string', () => {
      expect(decodeInviteCode('not-a-token')).toBeNull()
      expect(decodeInviteCode('a1b2c3')).toBeNull() // too short
    })

    it('returns null for raw JSON missing token', () => {
      expect(decodeInviteCode('{"space_id":"x"}')).toBeNull()
    })
  })
})

describe('bootstrap block (§D2b)', () => {
  it('round-trips the issuer key material and the GFS', () => {
    const payload = {
      token: 'a1b2c3d4e5f60718',
      space_id: 'sp-1',
      issuer_instance_id: 'ffffeeeeddddccccbbbb111122223333',
      issuer_identity_pk: 'aa'.repeat(32),
      issuer_keywrap_pk: 'bb'.repeat(32),
      issuer_keywrap_sig: 'c2ln',
      issuer_proto_version: 29,
      expires_at: '2026-12-01T00:00:00+00:00',
      via_gfs: { gfs_url: 'https://relay.example.org', gfs_space_id: 'g-1' },
    }
    expect(decodeInviteCode(buildInviteCode(payload))).toEqual(payload)
  })

  it('still decodes a code minted before the block existed', () => {
    const old = { token: 'a1b2c3d4e5f60718', space_id: 'sp-1' }
    expect(decodeInviteCode(buildInviteCode(old))).toEqual(old)
  })
})

describe('gfsBaseFromInviteUrl', () => {
  it('strips the /join/{token} tail to leave the server base', () => {
    expect(gfsBaseFromInviteUrl('https://relay.example.org/join/abc123'))
      .toBe('https://relay.example.org')
  })

  it('keeps a path prefix the server is mounted under', () => {
    expect(gfsBaseFromInviteUrl('https://example.org/gfs/join/abc123'))
      .toBe('https://example.org/gfs')
  })

  it('returns null for a URL that is not a join link', () => {
    expect(gfsBaseFromInviteUrl('https://relay.example.org/spaces/s1'))
      .toBeNull()
    expect(gfsBaseFromInviteUrl('https://relay.example.org/')).toBeNull()
  })

  it('returns null for something that is not a URL at all', () => {
    expect(gfsBaseFromInviteUrl('not a url')).toBeNull()
  })
})

describe('via_gfs without a gfs_space_id', () => {
  it('round-trips a published code carrying only the server base', () => {
    // The mint response has no per-server space id, so the SPA ships
    // ``gfs_url`` alone - the only field the redeem path reads.
    const payload = {
      token: 'a1b2c3d4e5f60718',
      via_gfs: { gfs_url: 'https://relay.example.org' },
    }
    expect(decodeInviteCode(buildInviteCode(payload))).toEqual(payload)
  })
})

describe('inviteLinkUrl — the short link the invite QR carries', () => {
  it('accepts a GFS /join/ link', () => {
    expect(inviteLinkUrl('https://gfs.example.org/join/AbC_d-9.x~'))
      .toBe('https://gfs.example.org/join/AbC_d-9.x~')
    expect(inviteLinkUrl('  https://GFS.example.org:8443/base/join/tok \n'))
      .toBe('https://gfs.example.org:8443/base/join/tok')
    expect(inviteLinkUrl('http://192.168.1.5:8765/join/tok'))
      .toBe('http://192.168.1.5:8765/join/tok')
  })

  it("maps the pinned viewer's join page to the same link", () => {
    expect(inviteLinkUrl(
      'https://viewer.social-home.io/v/abc123/#v=join&g=https%3A%2F%2Fgfs.example.org&t=tok',
    )).toBe('https://gfs.example.org/join/tok')
    // Another viewer page is not an invite.
    expect(inviteLinkUrl(
      'https://viewer.social-home.io/v/abc123/#v=space&g=https%3A%2F%2Fgfs.example.org&s=sp',
    )).toBeNull()
  })

  it('refuses anything that is not an invite link', () => {
    for (const raw of [
      '',
      'socialhome://invite#eyJ0b2tlbiI6ImFiYyJ9',
      'ftp://gfs.example.org/join/tok',
      'https://gfs.example.org/spaces/tok',
      'https://gfs.example.org/join/',
      'https://gfs.example.org/join/tok/',
      'https://gfs.example.org/join/..',
      'https://gfs.example.org/join/tok?x=1',
      'https://u:p@gfs.example.org/join/tok',
      'https://gfs.example.org\\@evil/join/tok',
      'https://gfs.example.org/join/t%20k',
    ]) {
      expect(inviteLinkUrl(raw), raw).toBeNull()
    }
  })

  it('is not mistaken for a code by decodeInviteCode', () => {
    expect(decodeInviteCode('https://gfs.example.org/join/tok')).toBeNull()
  })
})
