/**
 * Sharing household photos out as a copy (gallery D8) — the pure parts of
 * the share sheet: the targets the server offers, what fits where, the
 * audience line shown before anything is sent, and the result's toast.
 *
 * Server: ``GET /api/gallery/share/targets`` and ``POST /api/gallery/share``
 * (``socialhome/services/gallery_share_service.py``).
 */
import { t, isOne } from '@/i18n/i18n'
import { itemsWord, othersLine } from './galleryManage'

export interface ShareSpace {
  id: string
  name: string
  emoji: string | null
  space_type: string
  households: number
  /** May share as a post (``post_review``: the admins check it first). */
  post: boolean
  post_review: boolean
  /** The space's gallery is on: may share into a space album. */
  album: boolean
}

export interface ShareChat {
  id: string
  name: string
  group: boolean
  households: number
}

export interface ShareLimits {
  max_items: number
  post_items: number
  chat_items: number
  video_seconds: number
}

export interface ShareTargets {
  feed: boolean
  spaces: ShareSpace[]
  chats: ShareChat[]
  limits: ShareLimits
}

export interface ShareItem {
  id: string
  item_type: 'photo' | 'video'
  uploaded_by: string
  thumbnail_url: string
  caption?: string | null
  duration_s?: number | null
  processing?: boolean
}

export type SpaceMode = 'post' | 'album'

/** Where the copy goes — what the sheet has picked. */
export type ShareChoice =
  | { kind: 'feed' }
  | { kind: 'space'; space: ShareSpace; mode: SpaceMode }
  | { kind: 'chat'; chat: ShareChat }

export interface ShareResult {
  shared: number
  skipped: { item_id: string; reason: string }[]
  trimming: number
  queued: boolean
}

/** The server's cap for one share action (it refuses more). */
export const SHARE_MAX = 50

/** Why a target row can't take this selection, or ``null`` when it can.
 *  Only post and chat have small caps; a space album takes the whole
 *  share (≤ ``max_items``). */
export function tooManyFor(
  choice: { kind: 'feed' } | { kind: 'chat' } | { kind: 'space'; mode: SpaceMode },
  count: number,
  limits: ShareLimits,
): string | null {
  if (choice.kind === 'chat') {
    return count > limits.chat_items
      ? t('gallery.share.too_many_chat', { n: String(limits.chat_items) })
      : null
  }
  if (choice.kind === 'space' && choice.mode === 'album') {
    return count > limits.max_items
      ? t('gallery.share.too_many', { n: String(limits.max_items) })
      : null
  }
  return count > limits.post_items
    ? t('gallery.share.too_many_post', { n: String(limits.post_items) })
    : null
}

/** The videos longer than a space or chat takes (a minute). */
export function longVideos(items: readonly ShareItem[], limits: ShareLimits): ShareItem[] {
  return items.filter(i => i.item_type === 'video' && (i.duration_s ?? 0) > limits.video_seconds)
}

/** "4:10" — a video's length for the long-video note. */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/** The audience line, pinned with the button: who sees the copy. */
export function audienceLine(choice: ShareChoice): string {
  if (choice.kind === 'feed') return t('gallery.share.audience_feed')
  if (choice.kind === 'chat') {
    const { chat } = choice
    if (chat.households > 1) {
      return t('gallery.share.audience_chat_households', {
        name: chat.name, n: String(chat.households),
      })
    }
    return t('gallery.share.audience_chat', { name: chat.name })
  }
  const { space } = choice
  if (space.space_type === 'public' || space.space_type === 'global') {
    return t('gallery.share.audience_space_public', { name: space.name })
  }
  if (space.households <= 1) {
    return t('gallery.share.audience_space_home', { name: space.name })
  }
  return t('gallery.share.audience_space', { name: space.name, n: String(space.households) })
}

/** "Lena added 2 of these and will see that you shared them." — or
 *  ``null`` when every photo is the sharer's own. */
export function uploadersLine(
  items: readonly ShareItem[],
  me: string | undefined,
  nameOf: (userId: string) => string,
): string | null {
  const others = items.filter(i => i.uploaded_by && i.uploaded_by !== me)
  if (!others.length) return null
  const people = new Set(others.map(i => i.uploaded_by)).size
  const key = people > 1
    ? 'gallery.share.uploaders_many'
    : isOne(others.length) ? 'gallery.share.uploader_one' : 'gallery.share.uploader'
  return t(key, { names: othersLine(others, nameOf), n: String(others.length) })
}

/** The sheet's title: "Share 3 photos". */
export function shareTitle(items: readonly ShareItem[]): string {
  return t('gallery.share.title', { what: itemsWord(items) })
}

/** Where it went, for the toast: "the feed" / "“Grandparents”" / the chat. */
function placeName(choice: ShareChoice): string {
  if (choice.kind === 'feed') return t('gallery.share.place_feed')
  if (choice.kind === 'chat') return t('gallery.share.place_chat', { name: choice.chat.name })
  return t('gallery.share.place_space', { name: choice.space.name })
}

/** The toast after a share: what went, what is still being cut, what
 *  waits for review and what was left out (and why).
 *
 *  Keys by reason (listed so a grep finds them):
 *    gallery.share.skipped_private gallery.share.skipped_too_long
 *    gallery.share.skipped_processing gallery.share.skipped_not_found
 *    gallery.share.skipped_not_household */
export function resultMessage(result: ShareResult, choice: ShareChoice): string {
  const parts: string[] = []
  const where = placeName(choice)
  if (result.shared > 0) {
    parts.push(result.queued
      ? t('gallery.share.done_review', { where })
      : t(isOne(result.shared) ? 'gallery.share.done_one' : 'gallery.share.done', {
        n: String(result.shared), where,
      }))
  }
  if (result.trimming > 0) {
    parts.push(t(isOne(result.trimming) ? 'gallery.share.trimming_one' : 'gallery.share.trimming', {
      n: String(result.trimming),
    }))
  }
  const reasons = new Map<string, number>()
  for (const s of result.skipped) reasons.set(s.reason, (reasons.get(s.reason) ?? 0) + 1)
  for (const [reason, n] of reasons) {
    const key = `gallery.share.skipped_${reason}`
    const text = t(key, { n: String(n) })
    parts.push(text === key ? t('gallery.share.skipped_other', { n: String(n) }) : text)
  }
  return parts.join(' ') || t('gallery.share.nothing')
}

/** The request body for ``POST /api/gallery/share``. */
export function shareBody(
  ids: readonly string[],
  choice: ShareChoice,
  opts: { albumId?: string; newAlbumName?: string; text?: string; trim: boolean },
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    item_ids: [...ids],
    long_videos: opts.trim ? 'trim' : 'skip',
  }
  const text = opts.text?.trim()
  if (choice.kind === 'feed') {
    body.target = 'feed'
  } else if (choice.kind === 'chat') {
    body.target = 'chat'
    body.conversation_id = choice.chat.id
  } else if (choice.mode === 'post') {
    body.target = 'space_post'
    body.space_id = choice.space.id
  } else {
    body.target = 'space_album'
    body.space_id = choice.space.id
    if (opts.albumId) body.album_id = opts.albumId
    else body.new_album_name = opts.newAlbumName?.trim() ?? ''
  }
  if (text && !(choice.kind === 'space' && choice.mode === 'album')) body.text = text
  return body
}
