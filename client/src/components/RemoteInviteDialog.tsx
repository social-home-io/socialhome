/**
 * RemoteInviteDialog — unified "Add someone to this space" picker for
 * private-space admins. Surfaces both the inviter's own household
 * members AND every confirmed remote household's members in a single
 * typeahead over ``/api/friends``.
 *
 * On submit, the dialog dispatches to the right endpoint based on
 * whether the pick is local or remote:
 *
 *  - Local household member → ``POST /api/spaces/{id}/members``
 *    with ``{user_id}``. Immediate add — no token round-trip — because
 *    the user is already on this instance and the admin has authority
 *    to seat them.
 *  - Remote household member → ``POST /api/spaces/{id}/remote-invites``
 *    with ``{invitee_instance_id, invitee_user_id}`` (§D1b). The
 *    target household receives the invite over federation and the
 *    invitee accepts on their own instance.
 *
 * (Bug repro: prior versions only flattened ``data.households[]`` and
 * silently dropped ``data.instance``, so a household admin literally
 * could not pick their spouse who lived on the same instance.)
 */
import { useEffect, useMemo, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

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

interface PickRow {
  user_id: string
  instance_id: string
  display_name: string
  household_name: string
  last_seen_at: string | null
  /** ``true`` when the pick lives on the inviter's own instance — drives
   *  endpoint selection on submit (immediate add vs federated invite). */
  is_local: boolean
}

const open = signal<string | null>(null) // holds the space_id being invited to

export function openRemoteInviteDialog(spaceId: string) {
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
  const [pickedId, setPickedId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open.value) return
    setLoading(true)
    setError(null)
    api.get('/api/friends').then((raw) => {
      const data = raw as FriendsResponse
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
      setRows([...localRows, ...remoteRows])
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
    setQuery(''); setPickedId(null); setError(null)
  }

  const submit = async () => {
    const picked = rows.find((r) => rowKey(r) === pickedId)
    if (!picked) {
      setError(t('remote_invite.pick_first'))
      return
    }
    setSubmitting(true); setError(null)
    try {
      if (picked.is_local) {
        await api.post(`/api/spaces/${spaceId}/members`, {
          user_id: picked.user_id,
        })
        showToast(t('remote_invite.added', { name: picked.display_name }), 'success')
      } else {
        await api.post(`/api/spaces/${spaceId}/remote-invites`, {
          invitee_instance_id: picked.instance_id,
          invitee_user_id: picked.user_id,
        })
        showToast(t('remote_invite.sent', { name: picked.display_name }), 'success')
      }
      close()
    } catch (exc) {
      setError((exc as Error).message || t('remote_invite.send_failed'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal open={true} onClose={close} title={t('remote_invite.title')}>
      {loading ? (
        <p class="sh-muted">{t('remote_invite.loading')}</p>
      ) : rows.length === 0 ? (
        <p class="sh-muted">{t('remote_invite.empty')}</p>
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
               aria-label={t('remote_invite.list_aria')}>
            {matches.length === 0 ? (
              <p class="sh-muted sh-remote-invite-picker__empty">
                {t('remote_invite.no_matches')}
              </p>
            ) : (
              matches.map((r) => {
                const id = rowKey(r)
                const picked = pickedId === id
                return (
                  <button
                    key={id}
                    type="button"
                    role="option"
                    aria-selected={picked}
                    class={[
                      'sh-remote-invite-row',
                      picked ? 'sh-remote-invite-row--picked' : '',
                    ].filter(Boolean).join(' ')}
                    onClick={() => setPickedId(id)}
                    data-testid={`remote-invite-row-${r.user_id}`}
                  >
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
          <div class="sh-modal-actions">
            <Button variant="secondary" onClick={close} disabled={submitting}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="primary"
              onClick={submit}
              loading={submitting}
              disabled={!pickedId}
            >
              {(() => {
                const picked = rows.find((r) => rowKey(r) === pickedId)
                return picked && !picked.is_local
                  ? t('remote_invite.send')
                  : t('remote_invite.add')
              })()}
            </Button>
          </div>
        </>
      )}
    </Modal>
  )
}
