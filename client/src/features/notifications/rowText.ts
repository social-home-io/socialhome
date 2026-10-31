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
 * - ``notifications.row.<type>_others`` / ``_others_one``  a group game's
 *   row (v_76): ``{actor}`` and ``{count}`` other people (``params.others``
 *   — "Anna invited you and 3 others to play Quiz"); with no others the
 *   plain key
 * - ``notifications.row.<type>_times``  one person did it ``{count}`` times
 *   (``params.total`` > 1: "Ben invited you to play Chess 3 times"); the
 *   count is always two or more, so one plural form per locale
 *
 * ``{space}`` is the place's name: the space's, an app's for a game invite,
 * or a group chat's for a row about a chat (a missed call, being added). A
 * row without params (written before
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
  /** Has a ``_times`` key: one person did it again on the same row
   *  (``params.total`` > 1), counted as ``{count}``. */
  times?: boolean
  /** A group game's row: ``_others`` / ``_others_one`` keys, picked when
   *  ``params.others`` is a number above 0 (it wins over ``_many``). */
  others?: boolean
  /** Has no ``{actor}`` sentence (only ``_none`` / ``_count``). */
  noActor?: boolean
  /** Lives in a space or in the household feed: each key has an ``_in``
   *  sibling naming ``{space}``, used when the row is about a space. */
  inSpace?: boolean
  /** A chat row: each key has a ``_household`` sibling (the household
   *  chat) and an ``_in`` sibling naming ``{space}`` (a space's chat). A
   *  group chat's name is not in params: that row keeps its title. */
  chat?: boolean
}

/** Where a household-chat row leads (the feed's Chat tab; the server's
 *  ``HOUSEHOLD_CHAT_LINK``). */
export const HOUSEHOLD_CHAT_LINK = '/?tab=chat'

/**
 * The types the client renders from ``params``. A type left out keeps its
 * title: its sentence names something params don't carry (a task's title,
 * an event's summary, a decision, another household) or its wording
 * depends on more than params say (a dissolved space vs. one you were
 * removed from).
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
 *   notifications.row.post_replied notifications.row.post_replied_in
 *   notifications.row.post_replied_many notifications.row.post_replied_many_in
 *   notifications.row.post_replied_count notifications.row.post_replied_count_in
 *   notifications.row.comment_replied notifications.row.comment_replied_in
 *   notifications.row.comment_replied_many
 *   notifications.row.comment_replied_many_in
 *   notifications.row.comment_replied_count
 *   notifications.row.comment_replied_count_in
 *   notifications.row.post_reacted notifications.row.post_reacted_in
 *   notifications.row.post_reacted_many notifications.row.post_reacted_many_in
 *   notifications.row.post_reacted_count notifications.row.post_reacted_count_in
 *   notifications.row.space_invite notifications.row.space_invite_many
 *   notifications.row.call_missed notifications.row.call_missed_many
 *   notifications.row.call_missed_count notifications.row.call_missed_in
 *   notifications.row.call_missed_many_in notifications.row.call_missed_count_in
 *   notifications.row.chat_added notifications.row.chat_added_many
 *   notifications.row.chat_added_none notifications.row.chat_added_in
 *   notifications.row.chat_added_many_in notifications.row.chat_added_none_in
 *   notifications.row.poll_result notifications.row.poll_result_many
 *   notifications.row.poll_result_in notifications.row.poll_result_many_in
 *   notifications.row.poll_result_none notifications.row.poll_result_none_in
 *   notifications.row.poll_scheduled notifications.row.poll_scheduled_many
 *   notifications.row.poll_scheduled_in
 *   notifications.row.poll_scheduled_many_in
 *   notifications.row.report_answer_none
 *   notifications.row.report_answer_none_in
 *   notifications.row.dm_mention_in notifications.row.dm_mention_many_in
 *   notifications.row.dm_mention_household
 *   notifications.row.dm_mention_many_household
 *   notifications.row.space_join_approved_none
 *   notifications.row.space_join_denied_none
 *   notifications.row.space_ownership_transferred_none
 *   notifications.row.space_location_enabled_none
 *   notifications.row.space_admins_reconfirm_none
 *   notifications.row.post_moderated_none
 *   notifications.row.post_moderated_none_in
 *   notifications.row.bazaar_bid_placed_none
 *   notifications.row.bazaar_offer_accepted_none
 *   notifications.row.bazaar_offer_rejected_none
 *   notifications.row.app_challenge notifications.row.app_challenge_many
 *   notifications.row.app_challenge_continue notifications.row.app_challenge_continue_many
 *   notifications.row.app_challenge_waiting notifications.row.app_challenge_waiting_many
 *   notifications.row.app_challenge_waiting_times
 *   notifications.row.app_added notifications.row.app_added_many
 *   notifications.row.app_challenge_declined notifications.row.app_challenge_declined_many
 *   notifications.row.app_game_ended notifications.row.app_game_ended_many
 *   notifications.row.app_turn notifications.row.app_turn_many
 *   notifications.row.app_request notifications.row.app_request_many
 *   notifications.row.app_update_none
 *   notifications.row.app_challenge_others notifications.row.app_challenge_others_one
 *   notifications.row.app_challenge_waiting_others
 *   notifications.row.app_challenge_waiting_others_one
 *   notifications.row.app_table_joined notifications.row.app_table_joined_many
 *   notifications.row.app_table_joined_others notifications.row.app_table_joined_others_one
 *   notifications.row.app_table_started notifications.row.app_table_started_many
 *   notifications.row.app_table_started_others notifications.row.app_table_started_others_one
 *   notifications.row.app_table_can_start
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
  post_replied: { count: true, inSpace: true },
  comment_replied: { count: true, inSpace: true },
  post_reacted: { count: true, inSpace: true },
  space_invite: {},
  // A chat row's place is the group's name (``params.space_name``).
  call_missed: { count: true, inSpace: true },
  chat_added: { none: true, inSpace: true },
  // Nobody closed it: a poll that reached its deadline ("… has ended").
  poll_result: { none: true, inSpace: true },
  poll_scheduled: { inSpace: true },
  // Never who reviewed it or whom it was about.
  report_answer: { none: true, noActor: true, inSpace: true },
  dm_mention: { chat: true },
  space_join_approved: { none: true, noActor: true },
  space_join_denied: { none: true, noActor: true },
  space_ownership_transferred: { none: true, noActor: true },
  space_location_enabled: { none: true, noActor: true },
  space_admins_reconfirm: { none: true, noActor: true },
  post_moderated: { none: true, noActor: true, inSpace: true },
  bazaar_bid_placed: { none: true, noActor: true },
  bazaar_offer_accepted: { none: true, noActor: true },
  bazaar_offer_rejected: { none: true, noActor: true },
  // The app's name rides as the place (``params.space_name``): "Ben invited
  // you to play Chess". A row without it keeps its title.
  // A group game's invite (flag ``group``) counts the others asked:
  // "Anna invited you and 3 others to play Quiz".
  app_challenge: { others: true },
  // An old game picked up again: "Ben wants to continue your Chess game".
  app_challenge_continue: {},
  // An invite for an app the household doesn't have or turned off (D10):
  // the same sentence; Needs you says why it waits. One row per sender
  // (#1244): a repeat invite counts in ``params.total`` — "Ben invited you
  // to play Chess 3 times". A playable ``app_challenge`` is one row per
  // invite, so it never counts.
  app_challenge_waiting: { times: true, others: true },
  // A new app in the household: "Anna added Chess" — the app's name is
  // the place too.
  app_added: {},
  // "Ben declined your Chess game" — the app's name rides as the place too.
  app_challenge_declined: {},
  // "Ben no longer has Chess, so your game ended" (D9): the other household
  // uninstalled the app — the app's name rides as the place too.
  app_game_ended: {},
  // "Your turn with Ben in Chess" (2026-10-30): one row per game while
  // the app is closed — the app's name rides as the place too.
  app_turn: {},
  // Group games (v_76, design 3.7): "Ben and 2 others joined your Quiz
  // game" for the person who started it (one row per game; ``can_start``
  // adds "You can start now"), "Your Quiz game with Anna and 3 others
  // started" for each other player.
  app_table_joined: { others: true },
  app_table_started: { others: true },
  // A member asked for an app: "Milo and Kiki asked for Chess".
  app_request: {},
  // Admins: "Chess has an update" — nobody caused it; the app is the place.
  app_update: { none: true, noActor: true },
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
    'notifications.row.app_table_can_start',
  ]
  for (const [type, spec] of Object.entries(ROW_TYPES)) {
    const base = `notifications.row.${type}`
    const own: string[] = []
    if (!spec.noActor) own.push(base, `${base}_many`)
    if (spec.count) own.push(`${base}_count`)
    if (spec.none) own.push(`${base}_none`)
    if (spec.times) own.push(`${base}_times`)
    if (spec.others) own.push(`${base}_others`, `${base}_others_one`)
    if (spec.chat) {
      keys.push(...own.map(k => `${k}_in`), ...own.map(k => `${k}_household`))
      continue
    }
    keys.push(...own)
    if (spec.inSpace) keys.push(...own.map(k => `${k}_in`))
  }
  return keys
}

export interface RowInput {
  type: string
  title: string
  params?: NotifParams | null
  /** The space the row is about, if any (picks an ``_in`` sentence). */
  space_id?: string | null
  /** Where the row leads (tells the household chat apart). */
  link_url?: string | null
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
  } else if (spec.others && typeof p.others === 'number') {
    // A group game's row: the actor and how many others, never _many.
    key = p.others <= 0 ? base : isOne(p.others) ? `${base}_others_one` : `${base}_others`
  } else if (Math.max(p.actor_count, actors.length) > 1) key = `${base}_many`
  else if (spec.times && p.total > 1) key = `${base}_times`
  else key = base
  // ``_count`` counts the new things, ``_times`` every repeat on the row.
  const count = key === `${base}_times`
    ? p.total
    : key.startsWith(`${base}_others`) ? (p.others ?? 0) : p.new
  if (spec.chat) {
    if (row.link_url === HOUSEHOLD_CHAT_LINK) key = `${key}_household`
    else if (space) key = `${key}_in`
    else return null // a group chat (its name is in the title) or a nameless space
  } else if (spec.inSpace && (space || row.space_id)) {
    if (!space) return null // a space row whose name we don't know: the title names it
    key = `${key}_in`
  }
  if (!hasKey(key)) return null
  const template = t(key)
  if (template.includes('{space}') && !space) return null
  const segs = fill(template, {
    actor: actors.length > 0 ? [person(actors[0])] : [],
    actors: actorsPhrase(actors, p.actor_count),
    space: [{ kind: 'place', text: space }],
    count: [{ kind: 'text', text: String(count) }],
  })
  if (row.type === 'app_table_joined' && p.flags?.includes('can_start')) {
    // Its own sentence after the row's: enough players are in.
    segs.push({ kind: 'text', text: ` · ${t('notifications.row.app_table_can_start')}` })
  }
  return segs
}

/** The segments as one plain string (an accessible name, a short label). */
export function plainText(segs: Segment[]): string {
  return segs.map(s => s.text).join('')
}

/**
 * How a handled "Needs you" item ended, for its meta line: "Approved by
 * Søren", "You declined it", "Accepted", "No action needed". *viewerId*
 * turns the viewer's own answer into "You …". ``null`` while it waits.
 *
 * Keys (listed so a grep finds them): notifications.handled.approved_by
 *   notifications.handled.declined_by notifications.handled.by
 *   notifications.handled.approved_you notifications.handled.declined_you
 *   notifications.handled.by_you notifications.handled.accepted
 *   notifications.handled.declined notifications.handled.expired
 *   notifications.handled.withdrawn notifications.handled.done
 */
export function handledMeta(
  params: NotifParams | null | undefined,
  viewerId?: string | null,
): string | null {
  const h = params?.handled
  if (!h) return null
  const by = h.by
  const name = by?.name?.trim() || ''
  const verb = h.outcome === 'approved' || h.outcome === 'declined' ? h.outcome : 'by'
  if (by && viewerId && by.id === viewerId) {
    return t(verb === 'by' ? 'notifications.handled.by_you' : `notifications.handled.${verb}_you`)
  }
  if (by) {
    const who = name || t('notifications.someone')
    return t(verb === 'by' ? 'notifications.handled.by' : `notifications.handled.${verb}_by`, { name: who })
  }
  const outcome = ['accepted', 'declined', 'expired', 'withdrawn'].includes(h.outcome) ? h.outcome : 'done'
  return t(`notifications.handled.${outcome}`)
}

/** Every key :func:`handledMeta` may ask for (the locale tests walk it). */
export const HANDLED_KEYS: readonly string[] = [
  'notifications.handled.approved_by',
  'notifications.handled.declined_by',
  'notifications.handled.by',
  'notifications.handled.approved_you',
  'notifications.handled.declined_you',
  'notifications.handled.by_you',
  'notifications.handled.accepted',
  'notifications.handled.declined',
  'notifications.handled.expired',
  'notifications.handled.withdrawn',
  'notifications.handled.done',
]

/** "3 requests wait for you in Salon" — a space's grouped "Needs you"
 *  entry, as segments (the space as a place). */
export function groupSentence(count: number, space: string): Segment[] {
  const name = space.trim()
  if (!name) return [{ kind: 'text', text: t('notifications.needs_you.group_unnamed', { count: String(count) }) }]
  return fill(t('notifications.needs_you.group', { count: String(count) }), {
    space: [{ kind: 'place', text: name }],
  })
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
