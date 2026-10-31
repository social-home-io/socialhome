/**
 * GroupSheet — Social Home's own sheet to pick people for one group game
 * of an app (owner decisions D3 and D2, group play).
 *
 * An app asks with ``group.open`` (bridge.ts); the host draws this sheet
 * *outside* the app's frame, from Social Home's own read of who the person
 * may invite (``GET /invitable`` — never handed to the app). The person
 * picks, says how long the game stays open and may add a note, and taps
 * Send; Social Home opens the game (``POST /groups``) and the app gets back
 * only the game as a :type:`GroupSession` (seats and names, no usernames).
 *
 * - Only people whose household can take part in group games
 *   (``groups: true``) are listed; one quiet line says so when some were
 *   left out.
 * - Up to the app's ``players.max`` less the person opening it (owner D2,
 *   hard ceiling :data:`GROUP_MAX_PLAYERS`): search is always there, and
 *   "Everyone in <space>" picks a whole space in one tap (a second tap
 *   takes them out again).
 * - Send needs the app's fewest players, ignores taps for its first
 *   :data:`SEND_ARM_MS` (tap-jacking, as in InviteSheet) and names who the
 *   invites go to. Every word is Social Home's — the app passes no text.
 */
import { useEffect, useId, useMemo, useRef, useState } from 'preact/hooks'
import { isOne, t } from '@/i18n/i18n'
import { ApiError } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { getInvitable, type InvitablePerson, type InvitableSpace, type InviteCounts } from '@/store/apps'
import {
  createGroup,
  GROUP_NOTE_MAX,
  GROUP_OPEN_DAYS,
  type GroupOpenDays,
} from '@/store/appGroups'
import { GROUP_MAX_PLAYERS, type GroupSession } from '../groupBridge'
import type { GroupOpenRequest } from '../bridge'
import { SEND_ARM_MS, keyOf, limitFrom, limitOf, limitText, type InviteLimit } from '../inviteCaps'

/** How long a new game waits for people unless the person says otherwise. */
const DEFAULT_DAYS: GroupOpenDays = 3

function daysLabel(d: GroupOpenDays): string {
  switch (d) {
    case 1: return t('apps.group_sheet.days_1')
    case 3: return t('apps.group_sheet.days_3')
    case 7: return t('apps.group_sheet.days_7')
    case 14: return t('apps.group_sheet.days_14')
  }
}

/** Why Send didn't go through, said in the sheet. */
type Refusal =
  | { kind: 'size', min: number, max: number }
  | { kind: 'open_limit' | 'daily_limit', limit: number }
  | { kind: 'too_old' | 'not_supported' | 'failed' }
  | { kind: 'invites', limit: Exclude<InviteLimit, null> }

/** A refused Send in the sheet's terms. */
function refusalOf(err: unknown): Refusal {
  const caps = limitOf(err)
  if (caps) return { kind: 'invites', limit: caps }
  if (!(err instanceof ApiError)) return { kind: 'failed' }
  const params = (err.extra?.params ?? {}) as { min?: unknown, max?: unknown, limit?: unknown }
  const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) ? v : d)
  switch (err.code) {
    case 'APP_GROUP_SIZE':
      return { kind: 'size', min: num(params.min, 2), max: num(params.max, GROUP_MAX_PLAYERS) }
    case 'APP_GROUP_OPEN_LIMIT':
      return { kind: 'open_limit', limit: num(params.limit, 5) }
    case 'APP_GROUP_DAILY_LIMIT':
      return { kind: 'daily_limit', limit: num(params.limit, 10) }
    case 'APP_GROUP_HOUSEHOLD_TOO_OLD':
      return { kind: 'too_old' }
    case 'APP_GROUPS_NOT_SUPPORTED':
      return { kind: 'not_supported' }
    default:
      return { kind: 'failed' }
  }
}

/** The app's range, cut to what any group game may be. */
function rangeOf(players?: { min: number, max: number }): { min: number, max: number } {
  const max = Math.min(GROUP_MAX_PLAYERS, Math.max(2, Math.floor(players?.max ?? GROUP_MAX_PLAYERS)))
  const min = Math.min(max, Math.max(2, Math.floor(players?.min ?? 2)))
  return { min, max }
}

export function GroupSheet({ appId, appName, request, players, onDone }: {
  appId: string
  /** The app's name, as Social Home knows it (never the app's own text). */
  appName: string
  request: GroupOpenRequest
  /** The app's player range (manifest ``players``), when known. */
  players?: { min: number, max: number }
  /** The game opened, or ``null`` on Cancel. *refused* says why the app
   *  can't have one at all (``groups_not_supported``). */
  onDone: (session: GroupSession | null, refused?: 'groups_not_supported') => void
}) {
  const [people, setPeople] = useState<InvitablePerson[] | null>(null)
  const [spaces, setSpaces] = useState<{ space: InvitableSpace, keys: string[] }[]>([])
  const [leftOut, setLeftOut] = useState(false)
  const [counts, setCounts] = useState<InviteCounts | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [range, setRange] = useState(() => rangeOf(players))
  const [days, setDays] = useState<GroupOpenDays>(DEFAULT_DAYS)
  const [note, setNote] = useState('')
  const [sending, setSending] = useState(false)
  const [refusal, setRefusal] = useState<Refusal | null>(null)
  /** "Only 2 more fit" after a space didn't fit whole. */
  const [partial, setPartial] = useState<number | null>(null)
  /** When the list appeared: Send does nothing before SEND_ARM_MS. */
  const shownAt = useRef(Date.now())
  const done = useRef(false)
  const noteId = useId()

  /** Picks besides the person opening the game. */
  const cap = range.max - 1
  const need = range.min - 1

  const finish = (out: GroupSession | null) => {
    if (done.current) return
    done.current = true
    if (out === null && refusal?.kind === 'not_supported') onDone(null, 'groups_not_supported')
    else onDone(out)
  }

  useEffect(() => {
    let cancelled = false
    setLoadFailed(false)
    getInvitable(appId).then(r => {
      if (cancelled) return
      // The list (and suggested people) appear now and Send may become
      // live under the finger: the 600 ms count from here.
      shownAt.current = Date.now()
      const all = r.people ?? []
      const able = all.filter(p => p.groups === true)
      const ableKeys = new Set(able.map(keyOf))
      setPeople(able)
      setLeftOut(able.length < all.length)
      setCounts(r.invites)
      setSpaces((r.spaces ?? []).map(space => {
        const keys = [...new Set((space.people ?? [])
          .filter(i => Number.isInteger(i) && i >= 0 && i < all.length)
          .map(i => keyOf(all[i]))
          .filter(k => ableKeys.has(k)))]
        return { space, keys }
      }).filter(s => s.keys.length > 0))
      const suggested = [...new Set(request.suggest.map(keyOf).filter(k => ableKeys.has(k)))]
      setPicked(suggested.slice(0, rangeOf(players).max - 1))
    }).catch(() => {
      if (!cancelled) setLoadFailed(true)
    })
    return () => { cancelled = true }
    // The request is fixed for the sheet's life (one sheet per group.open).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId, attempt])

  const shown = useMemo(() => {
    const q = query.trim().toLocaleLowerCase()
    const list = people ?? []
    if (!q) return list
    return list.filter(p =>
      p.person_name.toLocaleLowerCase().includes(q)
      || p.display_name.toLocaleLowerCase().includes(q)
      || (p.household_name ?? '').toLocaleLowerCase().includes(q))
  }, [people, query])

  const chosen = (people ?? []).filter(p => picked.includes(keyOf(p)))
  const full = picked.length >= cap
  const capsHit = limitFrom(counts)
  const blocked = !!capsHit || refusal?.kind === 'not_supported'

  const changed = () => {
    setPartial(null)
    if (refusal && refusal.kind !== 'size' && refusal.kind !== 'not_supported') setRefusal(null)
  }

  const toggle = (p: InvitablePerson) => {
    const k = keyOf(p)
    changed()
    setPicked(cur => cur.includes(k)
      ? cur.filter(x => x !== k)
      : cur.length >= cap ? cur : [...cur, k])
  }

  const toggleSpace = (keys: string[]) => {
    changed()
    if (keys.every(k => picked.includes(k))) {
      setPicked(picked.filter(k => !keys.includes(k)))
      return
    }
    const missing = keys.filter(k => !picked.includes(k))
    const room = Math.max(0, cap - picked.length)
    setPicked([...picked, ...missing.slice(0, room)])
    if (missing.length > room && room > 0) setPartial(room)
  }

  const send = async () => {
    // Tap-jacking guard: a tap that lands before the person could have
    // read the sheet is not a decision.
    if (Date.now() - shownAt.current < SEND_ARM_MS) return
    if (sending || chosen.length < need || chosen.length > cap || blocked) return
    setSending(true)
    setRefusal(null)
    setPartial(null)
    const clean = note.trim()
    try {
      const session = await createGroup(appId, {
        targets: chosen.map(p => ({ instance_id: p.instance_id, user_ref: p.user_ref, is_local: p.is_local })),
        lobby_days: days,
        ...(clean ? { note: clean } : {}),
      })
      showToast(chosen.length === 1
        ? t('apps.group_sheet.sent_one', { person: chosen[0].person_name, name: appName })
        : t('apps.group_sheet.sent', { n: String(chosen.length), name: appName }))
      finish(session)
    } catch (err: unknown) {
      const r = refusalOf(err)
      if (r.kind === 'size') setRange({ min: Math.max(2, r.min), max: Math.max(2, Math.min(GROUP_MAX_PLAYERS, r.max)) })
      setRefusal(r)
      setSending(false)
    }
  }

  const refusalText = (r: Refusal): string => {
    switch (r.kind) {
      case 'size':
        return t('apps.group_sheet.err_size', { name: appName, min: String(r.min), max: String(r.max) })
      case 'open_limit':
        return t(isOne(r.limit) ? 'apps.group_sheet.err_open_limit_one' : 'apps.group_sheet.err_open_limit',
          { n: String(r.limit), name: appName })
      case 'daily_limit':
        return t(isOne(r.limit) ? 'apps.group_sheet.err_daily_limit_one' : 'apps.group_sheet.err_daily_limit',
          { n: String(r.limit) })
      case 'too_old':
        return t('apps.group_sheet.err_too_old')
      case 'not_supported':
        return t('apps.group_sheet.err_not_supported', { name: appName })
      case 'invites':
        return limitText(r.limit, counts, appName)
      case 'failed':
        return t('apps.group_sheet.err_failed')
    }
  }

  const sendLabel = chosen.length === 0
    ? t('apps.group_sheet.send_none')
    : chosen.length === 1
      ? t('apps.group_sheet.send_one', { name: chosen[0].person_name })
      : t('apps.group_sheet.send', { n: String(chosen.length) })

  const below = people !== null && people.length > 0 && chosen.length < need

  return (
    <Modal
      open
      onClose={() => finish(null)}
      title={t('apps.group_sheet.title', { name: appName })}
      class="sh-invite-sheet sh-group-sheet"
    >
      <p class="sh-invite-sheet__lead">{t('apps.group_sheet.lead', { name: appName })}</p>

      {people === null && !loadFailed && (
        <p class="sh-invite-sheet__status" role="status">{t('apps.invite.loading')}</p>
      )}

      {loadFailed && (
        <div class="sh-invite-sheet__status" role="alert">
          <p>{t('apps.invite.load_failed')}</p>
          <Button variant="secondary" onClick={() => setAttempt(n => n + 1)}>{t('common.try_again')}</Button>
        </div>
      )}

      {people !== null && people.length === 0 && (
        <p class="sh-invite-sheet__status">{t('apps.group_sheet.empty')}</p>
      )}

      {people !== null && people.length > 0 && (
        <>
          <input
            type="search"
            class="sh-input sh-invite-sheet__search"
            placeholder={t('apps.group_sheet.search')}
            aria-label={t('apps.group_sheet.search')}
            value={query}
            onInput={e => setQuery((e.target as HTMLInputElement).value)}
          />

          {spaces.length > 0 && (
            <div class="sh-group-sheet__spaces" role="group" aria-label={t('apps.group_sheet.from_space')}>
              {spaces.map(({ space, keys }) => {
                const on = keys.every(k => picked.includes(k))
                return (
                  <button
                    key={space.space_id}
                    type="button"
                    class={`sh-chip sh-group-sheet__space${on ? ' sh-chip--active' : ''}`}
                    aria-pressed={on ? 'true' : 'false'}
                    disabled={sending || (!on && full)}
                    onClick={() => toggleSpace(keys)}
                  >
                    {t('apps.group_sheet.everyone_in', { space: space.name, count: String(keys.length) })}
                  </button>
                )
              })}
            </div>
          )}

          <div class="sh-group-sheet__count" aria-live="polite">
            <span>{t('apps.group_sheet.picked', { n: String(chosen.length), max: String(cap) })}</span>
          </div>

          <fieldset class="sh-invite-sheet__people sh-group-sheet__people">
            <legend class="sr-only">{t('apps.invite.pick_up_to', { count: String(cap) })}</legend>
            {shown.length === 0 && <p class="sh-invite-sheet__status">{t('apps.invite.no_match')}</p>}
            {shown.map(p => {
              const k = keyOf(p)
              const on = picked.includes(k)
              return (
                <label key={k} class={`sh-invite-sheet__person${on ? ' is-on' : ''}`}>
                  <span aria-hidden="true"><Avatar name={p.person_name} size={36} /></span>
                  <span class="sh-invite-sheet__who">
                    <span class="sh-invite-sheet__name">{p.person_name}</span>
                    <span class="sh-invite-sheet__home">
                      {p.household_name ?? t('apps.invite.your_household')}
                    </span>
                  </span>
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={(!on && full) || sending}
                    onChange={() => toggle(p)}
                  />
                </label>
              )
            })}
          </fieldset>

          {full && !refusal && (
            <p class="sh-invite-sheet__hint">
              {t('apps.group_sheet.full', { name: appName, max: String(range.max) })}
            </p>
          )}
          {partial !== null && (
            <p class="sh-invite-sheet__hint" role="status">
              {t(isOne(partial) ? 'apps.group_sheet.only_more_one' : 'apps.group_sheet.only_more', { n: String(partial) })}
            </p>
          )}
          {below && (need > 1 || chosen.length > 0) && (
            <p class="sh-invite-sheet__hint">
              {t(isOne(need) ? 'apps.group_sheet.at_least_one' : 'apps.group_sheet.at_least', { n: String(need) })}
            </p>
          )}

          <div class="sh-group-sheet__fields">
            <label class="sh-group-sheet__field">
              <span class="sh-group-sheet__label">{t('apps.group_sheet.open_for')}</span>
              <select
                class="sh-input"
                value={String(days)}
                disabled={sending}
                onChange={e => setDays(Number((e.target as HTMLSelectElement).value) as GroupOpenDays)}
              >
                {GROUP_OPEN_DAYS.map(d => <option key={d} value={String(d)}>{daysLabel(d)}</option>)}
              </select>
            </label>
            <div class="sh-group-sheet__field sh-group-sheet__field--note">
              <label class="sh-group-sheet__label" for={noteId}>{t('apps.group_sheet.note')}</label>
              <input
                id={noteId}
                type="text"
                class="sh-input"
                maxLength={GROUP_NOTE_MAX}
                value={note}
                disabled={sending}
                onInput={e => setNote((e.target as HTMLInputElement).value.replace(/[\r\n]+/g, ' '))}
              />
              <span class="sh-group-sheet__counter" aria-hidden="true">
                {t('apps.group_sheet.note_count', { n: String(note.length), max: String(GROUP_NOTE_MAX) })}
              </span>
            </div>
          </div>
        </>
      )}

      {leftOut && <p class="sh-invite-sheet__hint">{t('apps.group_sheet.left_out')}</p>}

      {capsHit && !refusal && (
        <p class="sh-invite-sheet__limit" role="alert">{limitText(capsHit, counts, appName)}</p>
      )}
      {refusal && <p class="sh-invite-sheet__limit" role="alert">{refusalText(refusal)}</p>}

      <div class="sh-modal-actions sh-invite-sheet__actions">
        <Button variant="secondary" onClick={() => finish(null)}>{t('common.cancel')}</Button>
        <Button
          data-testid="group-send"
          loading={sending}
          disabled={chosen.length === 0 || chosen.length < need || chosen.length > cap || blocked}
          onClick={() => { void send() }}
        >
          {sendLabel}
        </Button>
      </div>
    </Modal>
  )
}
