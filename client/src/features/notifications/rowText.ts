/**
 * The sentence a notification row reads, in the viewer's language.
 *
 * Since 0129 a row carries ``params`` — who acted (newest first), how many
 * people and events, the space's name — and the client builds the sentence
 * from one whole-sentence key per type (``notifications.row.<type>`` and its
 * variants). Whole sentences, never stitched fragments: German puts the verb
 * last and French and Spanish move the object.
 *
 * Variants per type (each optional except the plain one):
 *
 * - ``notifications.row.<type>``        one person: ``{actor}``
 * - ``notifications.row.<type>_many``   two or more: ``{actors}`` (a list phrase
 *   — "Ben and Anna", "Ben, Anna and 3 others" — with the verb in the plural)
 * - ``notifications.row.<type>_count``  several new things: ``{count}`` and
 *   ``{actors}`` ("25 new posts in Familie Alpha from Ben, Anna and 3
 *   others"): it counts the things, never the people
 * - ``notifications.row.<type>_none``   nobody caused it (a review queue)
 *
 * ``{space}`` is the space's name. A row without params (written before
 * 0129), of a type with no key, or missing what its sentence needs keeps the
 * server's ``title`` — already in the viewer's language.
 *
 * The result is a list of segments, not HTML: names and space names are
 * text the renderer puts in ``<b>`` / ``<i>`` elements, so a name can never
 * inject markup.
 */
import { isOne, t } from '@/i18n/i18n'
import type { NotifActor, NotifParams } from '@/types'

export type { NotifActor, NotifParams }

/** A run of a sentence: plain text, a person's name or a place's name. */
export type Segment =
  | { kind: 'text', text: string }
  | { kind: 'person', text: string }
  | { kind: 'place', text: string }

interface TypeSpec {
  /** Has a ``_count`` key: several new things on one row ("25 new posts"). */
  count?: boolean
  /** Has a ``_none`` key: a row nobody caused. */
  none?: boolean
  /** Has no ``{actor}`` sentence (only ``_none`` / ``_count``). */
  noActor?: boolean
}

/**
 * The types the client renders from ``params``. A type left out keeps its
 * title: its sentence names something params don't carry (a task's title,
 * an event's summary, a decision) or nobody at all.
 *
 * Keys looked up from this table (listed so a grep finds them):
 *   notifications.row.post_created notifications.row.post_created_many
 *   notifications.row.post_created_count notifications.row.comment_added
 *   notifications.row.comment_added_many notifications.row.space_post_created
 *   notifications.row.space_post_created_many
 *   notifications.row.space_post_created_count
 *   notifications.row.space_comment_added
 *   notifications.row.space_comment_added_many
 *   notifications.row.space_mention notifications.row.space_mention_many
 *   notifications.row.space_here notifications.row.space_here_many
 *   notifications.row.dm_contact_requested
 *   notifications.row.dm_contact_requested_many
 *   notifications.row.space_member_joined
 *   notifications.row.space_member_joined_many
 *   notifications.row.space_join_requested
 *   notifications.row.space_join_requested_many
 *   notifications.row.space_remote_invite_accepted
 *   notifications.row.space_remote_invite_accepted_many
 *   notifications.row.space_remote_invite_declined
 *   notifications.row.space_remote_invite_declined_many
 *   notifications.row.moment_replied notifications.row.moment_replied_many
 *   notifications.row.moment_reacted notifications.row.moment_reacted_many
 *   notifications.row.highlight_reacted
 *   notifications.row.highlight_reacted_many notifications.row.user_followed
 *   notifications.row.user_followed_many
 *   notifications.row.moderation_pending_none
 *   notifications.row.space_report_none
 *   notifications.new_pill notifications.new_pill_one
 *   notifications.live_new notifications.live_new_one
 *   notifications.meta.new_of_total notifications.meta.new_of_total_one
 *   notifications.actors.one_more notifications.actors.one_more_one
 *   notifications.actors.more notifications.actors.more_one
 *   notifications.unread_empty_title notifications.unread_empty_body
 */
export const ROW_TYPES: Readonly<Record<string, TypeSpec>> = {
  post_created: { count: true },
  comment_added: {},
  space_post_created: { count: true },
  space_comment_added: {},
  space_mention: {},
  space_here: {},
  dm_contact_requested: {},
  space_member_joined: {},
  space_join_requested: {},
  space_remote_invite_accepted: {},
  space_remote_invite_declined: {},
  moment_replied: {},
  moment_reacted: {},
  highlight_reacted: {},
  user_followed: {},
  moderation_pending: { none: true, noActor: true },
  space_report: { none: true, noActor: true },
}

/** Every catalog key the renderer may ask for (the locale tests walk it). */
export function rowKeys(): string[] {
  const keys: string[] = [
    'notifications.actors.two',
    'notifications.actors.one_more',
    'notifications.actors.one_more_one',
    'notifications.actors.more',
    'notifications.actors.more_one',
    'notifications.someone',
  ]
  for (const [type, spec] of Object.entries(ROW_TYPES)) {
    const base = `notifications.row.${type}`
    if (!spec.noActor) keys.push(base, `${base}_many`)
    if (spec.count) keys.push(`${base}_count`)
    if (spec.none) keys.push(`${base}_none`)
  }
  return keys
}

export interface RowInput {
  type: string
  title: string
  params?: NotifParams | null
}

const PLACEHOLDER = /\{(\w+)\}/g

/** Fill *template*: each ``{name}`` becomes ``slots[name]``'s segments. */
function fill(template: string, slots: Record<string, Segment[]>): Segment[] {
  const out: Segment[] = []
  let last = 0
  for (const m of template.matchAll(PLACEHOLDER)) {
    const at = m.index ?? 0
    if (at > last) out.push({ kind: 'text', text: template.slice(last, at) })
    const slot = slots[m[1]]
    out.push(...(slot ?? [{ kind: 'text', text: m[0] }]))
    last = at + m[0].length
  }
  if (last < template.length) out.push({ kind: 'text', text: template.slice(last) })
  return out
}

function person(a: NotifActor): Segment {
  const name = a.name?.trim()
  return { kind: 'person', text: name || t('notifications.someone') }
}

/** "Ben", "Ben and Anna", "Ben, Anna and 3 others", "Ben and 2 others". */
export function actorsPhrase(actors: NotifActor[], actorCount: number): Segment[] {
  const shown = actors.slice(0, 2).map(person)
  const total = Math.max(actorCount, actors.length)
  if (shown.length === 0) return []
  if (shown.length === 1) {
    const others = total - 1
    if (others <= 0) return shown
    const key = isOne(others) ? 'notifications.actors.one_more_one' : 'notifications.actors.one_more'
    return fill(t(key, { n: String(others) }), { a: [shown[0]] })
  }
  const others = total - 2
  if (others <= 0) return fill(t('notifications.actors.two'), { a: [shown[0]], b: [shown[1]] })
  const key = isOne(others) ? 'notifications.actors.more_one' : 'notifications.actors.more'
  return fill(t(key, { n: String(others) }), { a: [shown[0]], b: [shown[1]] })
}

function hasKey(key: string): boolean {
  return t(key) !== key
}

/**
 * The row's sentence as segments, or ``null`` to show its ``title``.
 * *spaceName* is the space's live name when the client knows it (it wins
 * over the name the row was written with).
 */
export function rowSentence(row: RowInput, spaceName?: string | null): Segment[] | null {
  const spec = ROW_TYPES[row.type]
  const p = row.params
  if (!spec || !p) return null
  const base = `notifications.row.${row.type}`
  const actors = Array.isArray(p.actors) ? p.actors.filter(a => a && typeof a.id === 'string') : []
  const space = (spaceName || p.space_name || '').trim()
  let key: string
  if (spec.count && p.new > 1 && actors.length > 0) key = `${base}_count`
  else if (actors.length === 0 || spec.noActor) {
    if (!spec.none) return null
    key = `${base}_none`
  } else if (Math.max(p.actor_count, actors.length) > 1) key = `${base}_many`
  else key = base
  if (!hasKey(key)) return null
  const template = t(key)
  if (template.includes('{space}') && !space) return null
  const segs = fill(template, {
    actor: actors.length > 0 ? [person(actors[0])] : [],
    actors: actorsPhrase(actors, p.actor_count),
    space: [{ kind: 'place', text: space }],
    count: [{ kind: 'text', text: String(p.new) }],
  })
  return segs
}

/** The segments as one plain string (an accessible name, a short label). */
export function plainText(segs: Segment[]): string {
  return segs.map(s => s.text).join('')
}

/** "2 new · 5 in all" for a row that came back with new activity after it
 *  had been read (``total`` > ``new``); ``null`` otherwise. */
export function countsMeta(params: NotifParams | null | undefined): string | null {
  if (!params) return null
  const fresh = params.new
  const total = params.total
  if (!(total > fresh) || fresh < 1) return null
  const key = isOne(fresh) ? 'notifications.meta.new_of_total_one' : 'notifications.meta.new_of_total'
  return t(key, { new: String(fresh), total: String(total) })
}
