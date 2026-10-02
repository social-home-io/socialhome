/**
 * Space feature access levels (§4.3) in the SPA: what the settings offer,
 * the notes a read-only viewer sees, and the PEERS_TOO_OLD prompt.
 */
import { describe, it, expect } from 'vitest'
import {
  ACCESS_FEATURES,
  accessLevel,
  accessNote,
  announceSuppressedMessage,
  levelOptions,
  peersTooOldHouseholds,
} from './spaceAccess'

describe('accessLevel', () => {
  it('reads the feature level, defaulting to open', () => {
    const f = { posts_access: 'admin_only', pages_access: 'moderated' } as never
    expect(accessLevel(f, 'posts')).toBe('admin_only')
    expect(accessLevel(f, 'pages')).toBe('moderated')
    expect(accessLevel(f, 'tasks')).toBe('open')
    expect(accessLevel(undefined, 'calendar')).toBe('open')
  })

  it('treats an unknown value from a newer host as admin_only (fail closed)', () => {
    expect(accessLevel({ tasks_access: 'whatever' } as never, 'tasks')).toBe('admin_only')
  })
})

describe('levelOptions', () => {
  it('offers Reviewed for posts only', () => {
    expect(levelOptions('posts', 'open')).toEqual(['open', 'moderated', 'admin_only'])
    for (const f of ['pages', 'tasks', 'stickies', 'calendar'] as const) {
      expect(levelOptions(f, 'open')).toEqual(['open', 'admin_only'])
    }
  })

  it('keeps a moderated level set elsewhere visible, so a save never drops it', () => {
    expect(levelOptions('pages', 'moderated')).toEqual(['open', 'moderated', 'admin_only'])
  })
})

describe('accessNote', () => {
  it('has a note for every feature', () => {
    for (const f of ACCESS_FEATURES) {
      expect(accessNote(f)).toMatch(/admins/i)
    }
  })

  it('falls back to a generic note for an unknown feature', () => {
    expect(accessNote('gallery')).toMatch(/admins/i)
  })
})

describe('peersTooOldHouseholds', () => {
  it('reads the households of a 409 PEERS_TOO_OLD', () => {
    const err = {
      code: 'PEERS_TOO_OLD',
      extra: {
        households: [
          { instance_id: 'i-1', display_name: "Granny's house", proto_version: 41 },
          { instance_id: 'i-2', display_name: '', proto_version: 40 },
          'junk',
        ],
      },
    }
    expect(peersTooOldHouseholds(err)).toEqual([
      { instance_id: 'i-1', display_name: "Granny's house", proto_version: 41 },
      { instance_id: 'i-2', display_name: 'i-2', proto_version: 40 },
    ])
  })

  it('answers null for any other error', () => {
    expect(peersTooOldHouseholds(new Error('boom'))).toBeNull()
    expect(peersTooOldHouseholds({ code: 'FORBIDDEN', extra: {} })).toBeNull()
  })
})

describe('announceSuppressedMessage', () => {
  it('names the posts level that kept the feed card', () => {
    expect(announceSuppressedMessage({ announce_suppressed: true, announce_suppressed_reason: 'admin_only' }))
      .toBe('Saved — not announced in the feed, because only admins can post here.')
    expect(announceSuppressedMessage({ announce_suppressed: true, announce_suppressed_reason: 'moderated' }))
      .toBe('Saved — not announced in the feed, because posts here are reviewed.')
  })

  it('is null when nothing was dropped', () => {
    expect(announceSuppressedMessage({ id: 'e' })).toBeNull()
    expect(announceSuppressedMessage(undefined)).toBeNull()
  })
})
