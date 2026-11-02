import { describe, it, expect } from 'vitest'
import {
  audienceLine, clock, longVideos, resultMessage, shareBody, shareTitle, tooManyFor,
  uploadersLine, type ShareChoice, type ShareItem, type ShareLimits, type ShareSpace,
} from './gallerySharing'

const limits: ShareLimits = { max_items: 50, post_items: 5, chat_items: 5, video_seconds: 60 }

const space = (over: Partial<ShareSpace> = {}): ShareSpace => ({
  id: 's1', name: 'Grandparents', emoji: null, space_type: 'private', households: 3,
  post: true, post_review: false, album: true, ...over,
})

const photo = (id: string, by = 'u-me'): ShareItem => ({
  id, item_type: 'photo', uploaded_by: by, thumbnail_url: `api/media/${id}.webp`,
})

describe('gallerySharing', () => {
  it('says who sees the copy before anything is sent', () => {
    expect(audienceLine({ kind: 'feed' }))
      .toBe('A copy goes to your household feed. Everyone at home can see it.')
    expect(audienceLine({ kind: 'space', space: space(), mode: 'post' }))
      .toBe('A copy goes to the space “Grandparents”. The 3 households in it can see it.')
    expect(audienceLine({ kind: 'space', space: space({ households: 1 }), mode: 'album' }))
      .toBe('A copy goes to the space “Grandparents”. Only your household is in it.')
    expect(audienceLine({ kind: 'space', space: space({ space_type: 'public' }), mode: 'post' }))
      .toBe('A copy goes to the space “Grandparents”. Anyone in this public space can see it.')
    expect(audienceLine({ kind: 'chat', chat: { id: 'c', name: 'Oma Gisela', group: false, households: 2 } }))
      .toBe('A copy goes to the chat with Oma Gisela. 2 households are in it.')
    expect(audienceLine({ kind: 'chat', chat: { id: 'c', name: 'Ben', group: false, households: 1 } }))
      .toBe('A copy goes to the chat with Ben.')
  })

  it('tells who else added the photos — they will see it', () => {
    const name = (u: string) => ({ 'u-lena': 'Lena', 'u-ben': 'Ben' })[u] ?? ''
    expect(uploadersLine([photo('a'), photo('b')], 'u-me', name)).toBeNull()
    expect(uploadersLine([photo('a', 'u-lena'), photo('b', 'u-lena'), photo('c')], 'u-me', name))
      .toBe('Lena added 2 of these and will see that you shared them.')
    expect(uploadersLine([photo('a', 'u-lena')], 'u-me', name))
      .toBe('Lena added one of these and will see that you shared it.')
    expect(uploadersLine([photo('a', 'u-lena'), photo('b', 'u-ben')], 'u-me', name))
      .toBe('Lena and Ben added 2 of these and will see that you shared them.')
  })

  it('knows which targets can take this many', () => {
    expect(tooManyFor({ kind: 'feed' }, 5, limits)).toBeNull()
    expect(tooManyFor({ kind: 'feed' }, 6, limits)).toMatch(/up to 5/)
    expect(tooManyFor({ kind: 'space', mode: 'post' }, 6, limits)).toMatch(/space album/)
    expect(tooManyFor({ kind: 'space', mode: 'album' }, 50, limits)).toBeNull()
    expect(tooManyFor({ kind: 'space', mode: 'album' }, 51, limits)).toMatch(/50/)
    expect(tooManyFor({ kind: 'chat' }, 6, limits)).toMatch(/chat/)
  })

  it('finds videos over a minute and shows their length', () => {
    const items: ShareItem[] = [
      { ...photo('p') },
      { id: 'v1', item_type: 'video', uploaded_by: 'u', thumbnail_url: '', duration_s: 250 },
      { id: 'v2', item_type: 'video', uploaded_by: 'u', thumbnail_url: '', duration_s: 59 },
    ]
    expect(longVideos(items, limits).map(i => i.id)).toEqual(['v1'])
    expect(clock(250)).toBe('4:10')
    expect(clock(5)).toBe('0:05')
  })

  it('titles the sheet by what was picked', () => {
    expect(shareTitle([photo('a'), photo('b'), photo('c')])).toBe('Share 3 photos')
  })

  it('builds the request for each target', () => {
    const ids = ['a', 'b']
    expect(shareBody(ids, { kind: 'feed' }, { trim: true, text: ' Hi ' }))
      .toEqual({ item_ids: ids, long_videos: 'trim', target: 'feed', text: 'Hi' })
    expect(shareBody(ids, { kind: 'space', space: space(), mode: 'post' }, { trim: false }))
      .toEqual({ item_ids: ids, long_videos: 'skip', target: 'space_post', space_id: 's1' })
    expect(shareBody(ids, { kind: 'space', space: space(), mode: 'album' }, { trim: true, albumId: 'al', text: 'x' }))
      .toEqual({ item_ids: ids, long_videos: 'trim', target: 'space_album', space_id: 's1', album_id: 'al' })
    expect(shareBody(ids, { kind: 'space', space: space(), mode: 'album' }, { trim: true, newAlbumName: ' Oma ' }))
      .toEqual({ item_ids: ids, long_videos: 'trim', target: 'space_album', space_id: 's1', new_album_name: 'Oma' })
    expect(shareBody(ids, { kind: 'chat', chat: { id: 'c', name: 'B', group: false, households: 1 } }, { trim: true }))
      .toEqual({ item_ids: ids, long_videos: 'trim', target: 'chat', conversation_id: 'c' })
  })

  it('says what the share did, what waits and what was left out', () => {
    const toSpace: ShareChoice = { kind: 'space', space: space(), mode: 'post' }
    expect(resultMessage({ shared: 2, skipped: [], trimming: 0, queued: false }, toSpace))
      .toBe('2 copies shared to “Grandparents”.')
    expect(resultMessage({ shared: 1, skipped: [], trimming: 1, queued: false }, { kind: 'feed' }))
      .toBe("A copy was shared to the feed. The video's first minute is being prepared.")
    expect(resultMessage({ shared: 1, skipped: [], trimming: 0, queued: true }, toSpace))
      .toBe("Sent to “Grandparents”. The space's admins check it first.")
    expect(resultMessage({
      shared: 0,
      skipped: [{ item_id: 'a', reason: 'private' }, { item_id: 'b', reason: 'weird' }],
      trimming: 0,
      queued: false,
    }, { kind: 'feed' })).toBe(
      "1 from a private album weren't shared: only who added them can share them. 1 weren't shared.",
    )
    expect(resultMessage({ shared: 0, skipped: [], trimming: 0, queued: false }, { kind: 'feed' }))
      .toBe('Nothing was shared.')
  })
})
