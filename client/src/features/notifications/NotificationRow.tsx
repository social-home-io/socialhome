/**
 * NotificationRow — one row, the same on the Notifications page and in the
 * bell panel (§4 of the redesign).
 *
 *   [avatar]  Sentence: who · did what · where              ● (unread)
 *    └ glyph  time · "2 new · 5 in all"
 *
 * - **Avatar:** the newest actor's picture (initials without one), else the
 *   space's picture as a rounded square, else a plain tile with the glyph.
 * - **Glyph:** a small badge on the avatar saying what kind of thing it is.
 * - **Sentence:** built from ``params`` in the viewer's language
 *   (:func:`rowSentence`): names bold, the space emphasised, never cut.
 *   An app row's place is the app, named in the viewer's language
 *   (:func:`withAppName`), not the name stored with the row.
 *   A row without params shows its title.
 * - **Meta:** the relative time, plus "N new · M in all" on a row that came
 *   back with new activity after it was read. The place is never repeated.
 * - **Unread:** the dot (shape), a heavier weight and a tint — and the
 *   accessible name starts with "Unread," — never colour alone.
 *
 * The whole row is ONE control: a link to the thing (a button when there
 * is nothing to open). Opening it marks it read (:func:`markRowRead`).
 */
import type { ComponentChildren, JSX } from 'preact'
import { useEffect } from 'preact/hooks'
import { Avatar } from '@/components/Avatar'
import { api } from '@/api'
import { appHref } from '@/baseUrl'
import { formatLocale, isOne, t } from '@/i18n/i18n'
import { householdPictureUrl } from '@/store/householdUsers'
import { spaces } from '@/store/spaces'
import { spaceGlyph } from '@/features/spaces/spaceGlyph'
import { relativeDocsTime } from '@/utils/relativeTime'
import type { Notification } from '@/types'
import { currentUser } from '@/store/auth'
import { countsMeta, handledMeta, plainText, rowSentence, type Segment } from './rowText'
import { appIdOf, ensureAppCatalog, withAppName } from './appName'

export type GlyphName =
  | 'comment' | 'at' | 'post' | 'heart' | 'person' | 'calendar'
  | 'task' | 'shield' | 'tag' | 'mail' | 'bell' | 'phone' | 'poll' | 'game'

/** The kind of thing a type is about, for its glyph. */
export function glyphFor(type: string): GlyphName {
  if (type.startsWith('call_')) return 'phone'
  if (type === 'chat_added') return 'comment'
  if (type.startsWith('poll_')) return 'poll'
  if (type.startsWith('app_challenge') || type === 'app_added' || type === 'app_request' || type === 'app_update' || type === 'app_game_ended' || type === 'app_turn' || type.startsWith('app_table')) return 'game'
  if (type.includes('react')) return 'heart'
  if (type.includes('mention') || type === 'space_here') return 'at'
  if (type.includes('comment') || type.includes('replied')) return 'comment'
  if (type.endsWith('post_created')) return 'post'
  if (type === 'space_invite') return 'mail'
  if (/followed|joined|join_|invite|pairing/.test(type)) return 'person'
  if (type.startsWith('calendar')) return 'calendar'
  if (type.startsWith('task')) return 'task'
  if (/moderat|report/.test(type)) return 'shield'
  if (type.startsWith('bazaar')) return 'tag'
  if (type.startsWith('dm_')) return 'mail'
  return 'bell'
}

const GLYPHS: Record<GlyphName, JSX.Element> = {
  comment: <path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" />,
  at: <><circle cx="12" cy="12" r="4" /><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8" /></>,
  post: <><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" /><path d="M14 2v4a2 2 0 0 0 2 2h4M16 13H8M16 17H8" /></>,
  heart: <path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z" />,
  person: <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M19 8v6M22 11h-6" /></>,
  calendar: <><rect width="18" height="18" x="3" y="4" rx="2" /><path d="M16 2v4M8 2v4M3 10h18" /></>,
  task: <path d="M20 6 9 17l-5-5" />,
  shield: <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />,
  tag: <><path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z" /><circle cx="7.5" cy="7.5" r=".5" /></>,
  mail: <><rect width="20" height="16" x="2" y="4" rx="2" /><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" /></>,
  bell: <><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" /></>,
  phone: <path d="M13.832 16.568a1 1 0 0 0 1.213-.303l.355-.465A2 2 0 0 1 17 15h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2A18 18 0 0 1 2 4a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v3a2 2 0 0 1-.8 1.6l-.468.351a1 1 0 0 0-.292 1.233 14 14 0 0 0 6.392 6.384" />,
  poll: <path d="M3 3v16a2 2 0 0 0 2 2h16M18 17V9M13 17V5M8 17v-3" />,
  game: <><path d="M6 11h4M8 9v4M15 12h.01M18 10h.01" /><path d="M17.32 5H6.68a4 4 0 0 0-3.978 3.59C2.604 9.416 2 14.456 2 16a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.414-1.414A2 2 0 0 1 9.828 16h4.344a2 2 0 0 1 1.414.586L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.545-.604-6.584-.685-7.258A4 4 0 0 0 17.32 5z" /></>,
}

function Glyph({ name }: { name: GlyphName }) {
  return (
    <svg
      viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor"
      stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"
      aria-hidden="true" focusable="false"
    >
      {GLYPHS[name]}
    </svg>
  )
}

/** The live space row for *spaceId*, when the viewer has it. */
function spaceOf(spaceId: string | null | undefined) {
  if (!spaceId) return null
  return spaces.value.find(s => s.id === spaceId) ?? null
}

export function RowAvatar({ n, size }: { n: Notification, size: number }) {
  const glyph = glyphFor(n.type)
  const actor = n.params?.actors?.[0]
  const space = spaceOf(n.space_id)
  const spaceName = space?.name ?? n.params?.space_name ?? null
  let pic: JSX.Element
  if (actor) {
    pic = <Avatar src={householdPictureUrl(actor.id)} name={actor.name?.trim() || '?'} size={size} />
  } else if (n.space_id && spaceName) {
    pic = (
      <span class="sh-notif-av__space" style={{ width: `${size}px`, height: `${size}px` }}>
        {space?.icon_url
          ? <img src={space.icon_url} alt="" loading="lazy" />
          : spaceGlyph(space?.emoji ?? null, spaceName)}
      </span>
    )
  } else {
    pic = (
      <span class="sh-notif-av__sys" style={{ width: `${size}px`, height: `${size}px` }}>
        <Glyph name={glyph} />
      </span>
    )
  }
  return (
    <span class="sh-notif-av" aria-hidden="true">
      {pic}
      <span class="sh-notif-av__glyph" data-glyph={glyph}><Glyph name={glyph} /></span>
    </span>
  )
}

/** The sentence as elements: names in ``<b>``, the space in ``<i>``. Text
 *  only — a name can never become markup. */
export function Sentence({ segs }: { segs: Segment[] }) {
  return (
    <>
      {segs.map((s, i) => {
        if (s.kind === 'person') return <b key={i}>{s.text}</b>
        if (s.kind === 'place') return <i key={i}>{s.text}</i>
        return s.text
      })}
    </>
  )
}

interface RowProps {
  n: Notification
  /** Called when the row is opened (after the read is sent). */
  onOpen?: (n: Notification) => void
  /** Bell panel: a little smaller. */
  compact?: boolean
  /** The sentence's element id — inline actions point at it
   *  (``aria-describedby``) so "Approve" says what it approves. */
  titleId?: string
  /** A control beside the row (its "…" menu) — a sibling of the row's one
   *  link, never inside it. */
  menu?: ComponentChildren
}

export function NotificationRow({ n, onOpen, compact, titleId, menu }: RowProps) {
  const unread = !n.read_at
  const isApp = appIdOf(n) !== null
  useEffect(() => {
    // An app row names its app in the viewer's language (the catalog).
    if (isApp) ensureAppCatalog()
  }, [isApp])
  const segs = rowSentence(withAppName(n), spaceOf(n.space_id)?.name)
  // A handled "Needs you" item says how it ended ("Approved by Søren").
  const meta = handledMeta(n.params, currentUser.value?.user_id) ?? countsMeta(n.params)
  const cls = `sh-notif-row${unread ? ' sh-notif-row--unread' : ''}${compact ? ' sh-notif-row--compact' : ''}`
  const inner = (
    <>
      <RowAvatar n={n} size={compact ? 36 : 40} />
      <span class="sh-notif-content">
        {unread && <><span class="sr-only">{t('notifications.unread')},</span>{' '}</>}
        <span class="sh-notif-title" id={titleId}>{segs ? <Sentence segs={segs} /> : n.title}</span>
        {' '}
        <span class="sh-notif-meta">
          <time
            class="sh-notif-time"
            dateTime={n.created_at}
            title={new Date(n.created_at).toLocaleString(formatLocale())}
          >
            {relativeDocsTime(n.created_at)}
          </time>
          {meta && <><span class="sh-notif-meta__sep" aria-hidden="true">·</span>{' '}<span>{meta}</span></>}
        </span>
      </span>
      <span class="sh-notif-dot" aria-hidden="true" />
    </>
  )
  const onClick = () => { onOpen?.(n) }
  const row = n.link_url ? (
    <a class={cls} href={appHref(n.link_url)} data-notif-id={n.id} onClick={onClick}>
      {inner}
    </a>
  ) : (
    <button type="button" class={cls} data-notif-id={n.id} onClick={onClick}>
      {inner}
    </button>
  )
  if (!menu) return row
  return (
    <div class="sh-notif-item">
      {row}
      <div class="sh-notif-item__menu">{menu}</div>
    </div>
  )
}

/** *n*'s sentence as plain text (its title for a row without params). */
export function rowPlainText(n: Notification): string {
  const segs = rowSentence(withAppName(n), spaceOf(n.space_id)?.name)
  return segs ? plainText(segs) : n.title
}

/**
 * Mark *n* read on the server: every row about the same thing when it has
 * an ``object_key`` (``read-by-object`` — the comment row and the mention
 * row of one post clear together), else the row itself. The server answers
 * every device with the new unread count.
 */
export async function markRowRead(n: Notification): Promise<void> {
  if (n.object_key) {
    await api.post('/api/notifications/read-by-object', { object_keys: [n.object_key] })
  } else {
    await api.post(`/api/notifications/${n.id}/read`)
  }
}

/** *list* with *n* — and every row about the same thing — read now. */
export function withRead(list: Notification[], n: Notification): Notification[] {
  const now = new Date().toISOString()
  return list.map(x =>
    !x.read_at && (x.id === n.id || (n.object_key && x.object_key === n.object_key))
      ? { ...x, read_at: now }
      : x,
  )
}

/** The "↑ N new" pill: rows that arrived while the list was open. */
export function NewPill({ count, onShow }: { count: number, onShow: () => void }) {
  if (count <= 0) return null
  return (
    <div class="sh-notif-newpill">
      <button type="button" onClick={onShow}>
        <span aria-hidden="true">↑ </span>
        {t(isOne(count) ? 'notifications.new_pill_one' : 'notifications.new_pill', { n: String(count) })}
      </button>
    </div>
  )
}
