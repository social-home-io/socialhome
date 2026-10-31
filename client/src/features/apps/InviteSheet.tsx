/**
 * InviteSheet — Social Home's own invite sheet for an app (owner decision
 * D3, "invites go through Social Home").
 *
 * An app asks with ``people.pick`` (bridge.ts); the host draws this sheet
 * *outside* the app's frame, from Social Home's own read of who the person
 * may invite (``GET /invitable`` — never handed to the app). The person
 * picks and taps Send; Social Home opens the game(s) (``POST /sessions``)
 * and the app gets back only who was picked and the session ids.
 *
 * Tap-jacking: the app decides *when* the sheet opens, so it could open it
 * just as the person double-taps a button drawn in the same spot. Send
 * ignores activation for :data:`SEND_ARM_MS` after the sheet appears (the
 * browser-permission-prompt pattern), and always names who the invite goes
 * to ("Send invite to Ben"). A suggested person (a rematch) only starts
 * selected. Every word here is Social Home's — the app passes no text.
 *
 * The caps (10 unanswered invites, 30 a day, per person and app) are the
 * server's; the sheet says so before Send when it already knows, and in the
 * same plain words when the server refuses.
 */
import { useEffect, useMemo, useRef, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { ApiError } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import {
  continueGame,
  getInvitable,
  sendInvite,
  type InvitablePerson,
  type InviteCounts,
} from '@/store/apps'
import type { PickedPerson, PickRequest } from './bridge'
import { SEND_ARM_MS, keyOf, limitFrom, limitOf, limitText as capText, type InviteLimit } from './inviteCaps'

export { SEND_ARM_MS }

/** Show the search field from this many people on. */
const SEARCH_FROM = 6

type Limit = InviteLimit

export function InviteSheet({ appId, appName, request, onDone }: {
  appId: string
  /** The app's name, as Social Home knows it (never the app's own text). */
  appName: string
  request: PickRequest
  /** Who was picked and the games opened — ``[]`` on Cancel. */
  onDone: (picked: PickedPerson[]) => void
}) {
  const [people, setPeople] = useState<InvitablePerson[] | null>(null)
  const [counts, setCounts] = useState<InviteCounts | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [sending, setSending] = useState(false)
  const [refusal, setRefusal] = useState<Limit>(null)
  const [sendFailed, setSendFailed] = useState(false)
  const [taken, setTaken] = useState(false)
  /** When the sheet appeared: Send does nothing before SEND_ARM_MS. */
  const shownAt = useRef(Date.now())
  const done = useRef(false)
  const single = request.max <= 1
  /** "Continue game with Ben": one old game, the one person the app
   *  names — the person confirms (D3); nobody else is offered. */
  const continuing = request.purpose === 'continue'

  const finish = (out: PickedPerson[]) => {
    if (done.current) return
    done.current = true
    onDone(out)
  }

  useEffect(() => {
    let cancelled = false
    setLoadFailed(false)
    getInvitable(appId).then(r => {
      if (cancelled) return
      // The list (and a suggested person) appear now and Send may become
      // live under the finger: the 600 ms count from here.
      shownAt.current = Date.now()
      const s = request.suggest
      setPeople(continuing ? r.people.filter(p => !!s && keyOf(p) === keyOf(s)) : r.people)
      setCounts(r.invites)
      if (s && r.people.some(p => keyOf(p) === keyOf(s))) setPicked([keyOf(s)])
    }).catch(() => {
      if (!cancelled) setLoadFailed(true)
    })
    return () => { cancelled = true }
    // The request is fixed for the sheet's life (one sheet per people.pick).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId, attempt])

  const shown = useMemo(() => {
    const q = query.trim().toLocaleLowerCase()
    const list = people ?? []
    return q ? list.filter(p => p.display_name.toLocaleLowerCase().includes(q)) : list
  }, [people, query])

  const chosen = (people ?? []).filter(p => picked.includes(keyOf(p)))
  const limit: Limit = refusal ?? limitFrom(counts)

  const toggle = (p: InvitablePerson) => {
    const k = keyOf(p)
    setSendFailed(false)
    if (single) { setPicked([k]); return }
    setPicked(cur => cur.includes(k)
      ? cur.filter(x => x !== k)
      : cur.length >= request.max ? cur : [...cur, k])
  }

  const send = async () => {
    // Tap-jacking guard: a tap that lands before the person could have
    // read the sheet is not a decision.
    if (Date.now() - shownAt.current < SEND_ARM_MS) return
    if (sending || chosen.length === 0 || limit) return
    setSending(true)
    setSendFailed(false)
    setTaken(false)
    const out: PickedPerson[] = []
    try {
      for (const p of chosen) {
        const session_id = continuing && request.sessionId
          ? await continueGame(appId, request.sessionId, p)
          : await sendInvite(appId, p)
        out.push({
          instance_id: p.instance_id,
          user_ref: p.user_ref,
          is_local: p.is_local,
          display_name: p.display_name,
          session_id,
        })
      }
    } catch (err: unknown) {
      const capped = limitOf(err)
      if (out.length > 0) {
        // Some went out: the app gets those; say why the rest didn't.
        showToast(capped ? limitText(capped) : t('apps.invite.send_failed'), 'error')
        finish(out)
        return
      }
      if (capped) setRefusal(capped)
      else if (err instanceof ApiError && err.code === 'APP_SESSION_TAKEN') setTaken(true)
      else setSendFailed(true)
      setSending(false)
      return
    }
    showToast(continuing
      ? t('apps.invite.continued', { name: chosen[0].person_name })
      : out.length === 1
      ? t('apps.invite.sent_one', { name: chosen[0].person_name })
      : t('apps.invite.sent_many', { count: String(out.length) }))
    finish(out)
  }

  const limitText = (l: Exclude<Limit, null>) => capText(l, counts, appName)

  const sendLabel = continuing && chosen.length === 1
    ? t('apps.invite.continue_send', { name: chosen[0].person_name })
    : chosen.length === 0
    ? t('apps.invite.send')
    : chosen.length === 1
      ? t('apps.invite.send_to', { name: chosen[0].person_name })
      : t('apps.invite.send_many', { count: String(chosen.length) })

  return (
    <Modal
      open
      onClose={() => finish([])}
      title={t(continuing ? 'apps.invite.continue_title' : 'apps.invite.title', { name: appName })}
      class="sh-invite-sheet"
    >
      <p class="sh-invite-sheet__lead">
        {t(continuing ? 'apps.invite.continue_lead' : 'apps.invite.lead', { name: appName })}
      </p>

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
        <p class="sh-invite-sheet__status">
          {t(continuing ? 'apps.invite.continue_gone' : 'apps.invite.empty')}
        </p>
      )}

      {people !== null && people.length > 0 && (
        <>
          {!continuing && people.length >= SEARCH_FROM && (
            <input
              type="search"
              class="sh-input sh-invite-sheet__search"
              placeholder={t('apps.invite.search')}
              aria-label={t('apps.invite.search')}
              value={query}
              onInput={e => setQuery((e.target as HTMLInputElement).value)}
            />
          )}
          <fieldset class="sh-invite-sheet__people">
            <legend class="sr-only">
              {single ? t('apps.invite.pick_one') : t('apps.invite.pick_up_to', { count: String(request.max) })}
            </legend>
            {shown.length === 0 && <p class="sh-invite-sheet__status">{t('apps.invite.no_match')}</p>}
            {shown.map(p => {
              const k = keyOf(p)
              const on = picked.includes(k)
              const full = !single && !on && picked.length >= request.max
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
                    type={single ? 'radio' : 'checkbox'}
                    name={`sh-invite-${appId}`}
                    checked={on}
                    disabled={full || sending}
                    onChange={() => toggle(p)}
                  />
                </label>
              )
            })}
          </fieldset>
          {chosen.length === 1 && !limit && (
            <p class="sh-invite-sheet__hint">
              {t(continuing ? 'apps.invite.continue_hint' : 'apps.invite.hint', { name: chosen[0].person_name })}
            </p>
          )}
        </>
      )}

      {limit && <p class="sh-invite-sheet__limit" role="alert">{limitText(limit)}</p>}
      {sendFailed && <p class="sh-invite-sheet__limit" role="alert">{t('apps.invite.send_failed')}</p>}
      {taken && <p class="sh-invite-sheet__limit" role="alert">{t('apps.invite.taken')}</p>}

      <div class="sh-modal-actions sh-invite-sheet__actions">
        <Button variant="secondary" onClick={() => finish([])}>{t('common.cancel')}</Button>
        <Button
          data-testid="invite-send"
          loading={sending}
          disabled={chosen.length === 0 || !!limit}
          onClick={() => { void send() }}
        >
          {sendLabel}
        </Button>
      </div>
    </Modal>
  )
}
