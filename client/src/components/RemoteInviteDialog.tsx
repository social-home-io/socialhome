/**
 * RemoteInviteDialog — unified "Invite people to this space" picker for
 * private-space admins. Surfaces both the inviter's own household
 * members AND every confirmed remote household's members in a single
 * typeahead over ``/api/friends``.
 *
 Everyone picked gets an invite they accept or decline — someone in
 * the inviter's own household exactly like someone in another one; the
 * dialog says "Send invite" / "Invite sent" for both. Only the endpoint
 * differs:
 *
 *  - Local household member → ``POST /api/spaces/{id}/members``
 *    with ``{user_id}``: a pending local invite (``invite_local_user``),
 *    shown on ``GET /api/local_invites``.
 *  - Remote household member → ``POST /api/spaces/{id}/remote-invites``
 *    with ``{invitee_instance_id, invitee_user_id}`` (§D1b). The
 *    target household receives the invite over federation and the
 *    invitee accepts on their own instance.
 *
 * People already in the space (``GET /api/spaces/{id}/members``) are
 * not offered.
 *
 * (Bug repro: prior versions only flattened ``data.households[]`` and
 * silently dropped ``data.instance``, so a household admin literally
 * could not pick their spouse who lived on the same instance.)
 *
 * It is the space's ONE "Invite" door: someone who isn't on the list (a
 * household you aren't paired with) gets an invite link — "Share an
 * invite link instead" hands over to ``SpaceInviteDialog``.
 */
import { useEffect, useMemo, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import { openSpaceInvite } from './SpaceInviteDialog'
import type { SpaceRole } from '@/features/spaces/spaceRoles'
import { formatLocale, t } from '@/i18n/i18n'

interface FriendsHouseholdMember {
  user_id: string
  instance_id?: string  // present on remote rows
  remote_username?: string
  display_name: string
  last_seen_at?: string | null
}

interface FriendsHousehold {
  instance_id: string
  display_name: string
  status: string
  reachable: boolean
  members: FriendsHouseholdMember[]
}

interface FriendsResponse {
  instance: {
    instance_id: string
    display_name: string
    members: FriendsHouseholdMember[]
  }
  households: FriendsHousehold[]
}

/** A row of ``GET /api/spaces/{id}/members`` — ``instance_id`` only on
 *  a member from another household. */
interface SpaceMemberRow {
  user_id: string
  instance_id?: string | null
}

interface PickRow {
  user_id: string
  instance_id: string
  display_name: string
  household_name: string
  last_seen_at: string | null
  /** ``true`` when the pick lives on the inviter's own instance — drives
   *  endpoint selection on submit (local vs federated invite). */
  is_local: boolean
}

const open = signal<string | null>(null) // holds the space_id being invited to
/** The opener's role — decides which roles an invite link may hand out. */
const openerRole = signal<SpaceRole | undefined>(undefined)

export function openRemoteInviteDialog(spaceId: string, role?: SpaceRole) {
  openerRole.value = role
  open.value = spaceId
}

/** "last seen …" line for a row, or "never seen". */
function lastSeenLabel(iso: string | null): string {
  if (!iso) return t('remote_invite.never_seen')
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return t('remote_invite.never_seen')
  const min = Math.floor((Date.now() - at) / 60_000)
  const hr = Math.floor(min / 60)
  const day = Math.floor(hr / 24)
  const when = min < 1 ? t('remote_invite.just_now')
    : min < 60 ? t('remote_invite.minutes_ago', { n: String(min) })
    : hr < 24 ? t('remote_invite.hours_ago', { n: String(hr) })
    : day < 7 ? t('remote_invite.days_ago', { n: String(day) })
    : t('remote_invite.weeks_ago', { n: String(Math.floor(day / 7)) })
  return t('remote_invite.last_seen', { when })
}

export function RemoteInviteDialog() {
  const [rows, setRows] = useState<PickRow[]>([])
  const [query, setQuery] = useState('')
  /** Rows picked (``rowKey``) — several people get their invites at once. */
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set())
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open.value) return
    setLoading(true)
    setError(null)
    const spaceId = open.value
    Promise.all([
      api.get('/api/friends'),
      // Fail-soft: without the member list everyone is offered, and the
      // server refuses someone who is already in.
      api.get(`/api/spaces/${spaceId}/members`).catch(() => []),
    ]).then(([raw, rawMembers]) => {
      const data = raw as FriendsResponse
      const inSpace = new Set(
        ((rawMembers as SpaceMemberRow[] | null) ?? []).map(
          (m) => `${m.instance_id ?? data.instance.instance_id}:${m.user_id}`,
        ),
      )
      const meId = currentUser.value?.user_id
      const localRows: PickRow[] = (data.instance?.members || [])
        // Hide the inviter themselves — they're already in the space
        // by virtue of being its admin / creator.
        .filter((m) => m.user_id !== meId)
        .map((m) => ({
          user_id: m.user_id,
          instance_id: data.instance.instance_id,
          display_name: m.display_name,
          household_name: data.instance.display_name,
          last_seen_at: m.last_seen_at ?? null,
          is_local: true,
        }))
      const remoteRows: PickRow[] = (data.households || [])
        .filter((h) => h.status === 'confirmed' || h.status === 'active')
        .flatMap((h) => h.members.map((m) => ({
          user_id: m.user_id,
          // For confirmed remote members ``instance_id`` is on the row;
          // fall back to the household's id (always present).
          instance_id: m.instance_id ?? h.instance_id,
          display_name: m.display_name,
          household_name: h.display_name,
          last_seen_at: m.last_seen_at ?? null,
          is_local: false,
        })))
      // Local first — household-mates are the most common pick.
      setRows([...localRows, ...remoteRows].filter(
        (r) => !inSpace.has(`${r.instance_id}:${r.user_id}`),
      ))
    }).catch(() => {
      setRows([])
    }).finally(() => setLoading(false))
  }, [open.value])

  // Stable composite-key the picker uses to identify a row, since
  // ``user_id`` alone is not unique across households.
  const rowKey = (r: PickRow) => `${r.instance_id}:${r.user_id}`

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return rows
    return rows.filter((r) => (
      r.display_name.toLowerCase().includes(q)
      || r.household_name.toLowerCase().includes(q)
    ))
  }, [rows, query])

  if (!open.value) return null

  const spaceId = open.value
  const close = () => {
    open.value = null
    setQuery(''); setPicked(new Set()); setError(null)
  }

  const shareLink = () => {
    close()
    openSpaceInvite(spaceId, null, openerRole.value)
  }
  const linkRow = (
    <p class="sh-remote-invite-link">
      {t('remote_invite.not_listed')}{' '}
      <button type="button" class="sh-link-button" onClick={shareLink}>
        {t('remote_invite.share_link')}
      </button>
    </p>
  )

  const toggle = (id: string) => {
    const next = new Set(picked)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setPicked(next)
  }

  /** One invite: a local one for someone in this household, a §D1b one
   *  for someone in another — both wait for the person to accept. */
  const invite = (r: PickRow) => (r.is_local
    ? api.post(`/api/spaces/${spaceId}/members`, { user_id: r.user_id })
    : api.post(`/api/spaces/${spaceId}/remote-invites`, {
      invitee_instance_id: r.instance_id,
      invitee_user_id: r.user_id,
    }))

  const submit = async () => {
    const chosen = rows.filter((r) => picked.has(rowKey(r)))
    if (chosen.length === 0) {
      setError(t('remote_invite.pick_first'))
      return
    }
    setSubmitting(true); setError(null)
    // Every invite at once; each is its own request, so one that fails
    // (an unreachable household) doesn't hold up the others.
    const results = await Promise.allSettled(chosen.map(invite))
    setSubmitting(false)
    const sent = chosen.filter((_, i) => results[i].status === 'fulfilled')
    const failed = chosen.filter((_, i) => results[i].status === 'rejected')
    if (sent.length === 1) {
      showToast(t('remote_invite.sent', { name: sent[0].display_name }), 'success')
    } else if (sent.length > 1) {
      showToast(t('remote_invite.sent_n', { n: String(sent.length) }), 'success')
    }
    if (failed.length === 0) {
      close()
      return
    }
    // Who got theirs leaves the list; who didn't stays picked for "Try
    // again".
    const sentKeys = new Set(sent.map(rowKey))
    setRows(rows.filter((r) => !sentKeys.has(rowKey(r))))
    setPicked(new Set(failed.map(rowKey)))
    if (chosen.length === 1) {
      const exc = (results[0] as PromiseRejectedResult).reason as Error | undefined
      setError(exc?.message || t('remote_invite.send_failed'))
    } else {
      setError(t('remote_invite.send_failed_for', {
        names: new Intl.ListFormat(formatLocale(), { type: 'conjunction' })
          .format(failed.map((r) => r.display_name)),
      }))
    }
  }

  return (
    <Modal open={true} onClose={close} title={t('remote_invite.title')}>
      {loading ? (
        <p class="sh-muted">{t('remote_invite.loading')}</p>
      ) : rows.length === 0 ? (
        <>
          <p class="sh-muted">{t('remote_invite.empty')}</p>
          <div class="sh-modal-actions">
            <Button variant="secondary" onClick={close}>{t('common.cancel')}</Button>
            <Button onClick={shareLink}>{t('remote_invite.share_link')}</Button>
          </div>
        </>
      ) : (
        <>
          <label class="sh-form-field">
            <span>{t('remote_invite.find_label')}</span>
            <input
              type="search"
              value={query}
              placeholder={t('remote_invite.search_placeholder')}
              onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
              autoFocus
              data-testid="remote-invite-search"
            />
          </label>
          <div class="sh-remote-invite-picker" role="listbox"
               aria-multiselectable="true"
               aria-label={t('remote_invite.list_aria')}>
            {matches.length === 0 ? (
              <p class="sh-muted sh-remote-invite-picker__empty">
                {t('remote_invite.no_matches')}
              </p>
            ) : (
              matches.map((r) => {
                const id = rowKey(r)
                const on = picked.has(id)
                return (
                  <button
                    key={id}
                    type="button"
                    role="option"
                    aria-selected={on}
                    class={[
                      'sh-remote-invite-row',
                      on ? 'sh-remote-invite-row--picked' : '',
                    ].filter(Boolean).join(' ')}
                    onClick={() => toggle(id)}
                    data-testid={`remote-invite-row-${r.user_id}`}
                  >
                    <span class="sh-remote-invite-row__check" aria-hidden="true">
                      {on ? '✓' : ''}
                    </span>
                    <span class="sh-remote-invite-row__name">
                      {r.display_name}
                    </span>
                    <span class="sh-remote-invite-row__meta">
                      {r.household_name} · {lastSeenLabel(r.last_seen_at)}
                    </span>
                  </button>
                )
              })
            )}
          </div>
          {error && <p class="sh-error">{error}</p>}
          {linkRow}
          <div class="sh-modal-actions">
            <Button variant="secondary" onClick={close} disabled={submitting}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="primary"
              onClick={submit}
              loading={submitting}
              disabled={picked.size === 0}
            >
              {picked.size > 1
                ? t('remote_invite.send_n', { n: String(picked.size) })
                : t('remote_invite.send')}
            </Button>
          </div>
        </>
      )}
    </Modal>
  )
}
