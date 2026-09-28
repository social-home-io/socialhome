/**
 * NewDmDialog — start a new DM or group DM (§23.47b).
 *
 * UX shape:
 *   • Picker is built from ``GET /api/friends`` — local household
 *     members PLUS every paired remote household's members. Remote
 *     rows are tagged with the household name ("Brother's house")
 *     so the user can find their brother without leaving the
 *     dialog and the search input matches the household label too.
 *   • Click a row to toggle membership; selected users appear as
 *     removable chips above the list. Empty selection disables Start.
 *   • One member selected → POST /api/conversations/dm — body is
 *     ``{username}`` for a local pick or ``{user_id}`` for a remote
 *     pick (the backend route accepts either; remote routing rides
 *     the federation envelope path automatically).
 *   • Two or more selected → POST /api/conversations/group with
 *     ``{members: [local usernames], member_user_ids: [remote user_ids]}``.
 *     Groups can span households (v_37): this household becomes the
 *     group's home. A household whose Social Home is too old for group
 *     chats (``supports_group_dm: false`` on ``/api/friends``) can only
 *     be messaged 1:1 — its rows grey out once a group is forming, and
 *     the row says why.
 *
 * On successful create the dialog navigates straight into the new
 * thread — "create" and "open" feel like one action, not two.
 */
import { signal, computed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { Avatar } from './Avatar'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'

/** A row in the DM picker — local or remote household member. ``username``
 *  is only meaningful for local rows; remote rows carry their
 *  ``user_id`` + ``instance_id`` and the backend's
 *  ``POST /api/conversations/dm`` 1:1 path uses ``user_id`` to route
 *  the federation envelope. */
export interface Pickable {
  user_id: string
  username: string  // remote: ``ru.remote_username`` from /api/friends
  display_name: string
  picture_url: string | null
  /** Local rows: ``null``. Remote rows: the paired peer's
   *  ``instance_id``. Drives the local-vs-remote split on submit. */
  instance_id: string | null
  /** The household label rendered next to the name on remote rows so
   *  the user can disambiguate "Alice in my house" vs "Alice in
   *  Brother's house" without picking the wrong one. ``null`` for
   *  local rows (own household label is implicit). */
  household_name: string | null
  /** Whether this person can be put in a group chat — always for local
   *  rows; for remote rows the household must be v_37+. */
  supports_group: boolean
}

const open       = signal(false)
const users      = signal<Pickable[]>([])
/** Set of ``user_id``s selected for the new conversation. Empty until
 *  the user picks at least one. The size of this set drives the
 *  one-vs-group endpoint routing on submit. ``user_id`` is the
 *  globally-unique key — username collides across households. */
const picked     = signal<Set<string>>(new Set())
const groupName  = signal('')
const search     = signal('')
const loading    = signal(false)

/** All pickable rows minus the viewer themselves. */
const recipients = computed<Pickable[]>(() => {
  const me = currentUser.value?.user_id
  return users.value.filter((u) => u.user_id !== me)
})

const filtered = computed<Pickable[]>(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return recipients.value
  return recipients.value.filter(u => {
    if (u.display_name.toLowerCase().includes(q)) return true
    if (u.username.toLowerCase().includes(q)) return true
    if (u.household_name?.toLowerCase().includes(q)) return true
    return false
  })
})

const pickedUsers = computed<Pickable[]>(() => {
  const set = picked.value
  return recipients.value.filter(u => set.has(u.user_id))
})

const isGroup = computed(() => picked.value.size >= 2)

/** True when a picked person can only be messaged 1:1 (their household
 *  can't take part in group chats yet). Blocks Start for a group. */
const hasGroupBlockerPicked = computed(() =>
  pickedUsers.value.some(u => !u.supports_group),
)

function reset() {
  picked.value = new Set()
  groupName.value = ''
  search.value = ''
}

function togglePick(user_id: string) {
  const next = new Set(picked.value)
  if (next.has(user_id)) {
    next.delete(user_id)
  } else {
    next.add(user_id)
  }
  picked.value = next
}

interface FriendsResponseMember {
  user_id: string
  username?: string  // local users
  remote_username?: string  // remote users
  display_name: string
  picture_url: string | null
}

interface FriendsResponseHousehold {
  instance_id: string | null
  display_name: string
  /** v_37+ households only; absent from older backends (read as false). */
  supports_group_dm?: boolean
  members: FriendsResponseMember[]
}

export interface FriendsResponse {
  instance: FriendsResponseHousehold
  households: FriendsResponseHousehold[]
}

export function flattenFriends(payload: FriendsResponse): Pickable[] {
  const out: Pickable[] = []
  for (const m of payload.instance.members) {
    out.push({
      user_id: m.user_id,
      username: m.username ?? '',
      display_name: m.display_name,
      picture_url: m.picture_url,
      instance_id: null,
      household_name: null,
      supports_group: true,
    })
  }
  for (const h of payload.households) {
    for (const m of h.members) {
      out.push({
        user_id: m.user_id,
        username: m.remote_username ?? m.username ?? '',
        display_name: m.display_name,
        picture_url: m.picture_url,
        instance_id: h.instance_id,
        household_name: h.display_name,
        supports_group: h.supports_group_dm === true,
      })
    }
  }
  return out
}

/** Why a greyed-out row can't be picked — shown on the row itself (touch
 *  devices have no hover tooltip). */
function disabledReason(row: Pickable): string {
  if (!row.supports_group) {
    return `${row.household_name ?? 'Their household'} needs a Social Home update for group chats — 1:1 only`
  }
  return 'Can’t share a group with the person picked above — their household needs an update'
}

export function openNewDm() {
  reset()
  open.value = true
  api.get('/api/friends').then((data: FriendsResponse) => {
    users.value = flattenFriends(data)
  })
}

export function NewDmDialog({ onCreated }: { onCreated?: (convId: string) => void }) {
  const location = useLocation()

  const handleCreate = async () => {
    if (loading.value) return
    const picks = pickedUsers.value
    if (picks.length === 0) return
    loading.value = true
    try {
      let conv: { id: string }
      if (picks.length === 1) {
        const target = picks[0]
        // Remote (paired-peer member) → use ``user_id`` so the backend's
        // 1:1 path routes via the federation envelope. Local → use
        // ``username`` (back-compat with the existing route shape).
        const body = target.instance_id !== null
          ? { user_id: target.user_id }
          : { username: target.username }
        conv = await api.post('/api/conversations/dm', body)
        showToast('Conversation started', 'success')
      } else {
        // Local people by username, people from other households by
        // ``user_id`` — the backend seats them and ships the member list
        // to their households.
        const body: { members: string[]; member_user_ids?: string[]; name?: string } = {
          members: picks.filter(p => p.instance_id === null).map(p => p.username),
        }
        const remoteIds = picks.filter(p => p.instance_id !== null).map(p => p.user_id)
        if (remoteIds.length > 0) body.member_user_ids = remoteIds
        const trimmedName = groupName.value.trim()
        if (trimmedName) body.name = trimmedName
        conv = await api.post('/api/conversations/group', body)
        showToast(
          trimmedName ? `Group "${trimmedName}" created` : 'Group created',
          'success',
        )
      }
      open.value = false
      reset()
      if (onCreated) {
        onCreated(conv.id)
      } else {
        location.route(`/dms/${conv.id}`)
      }
    } catch (e: any) {
      showToast(e.message || 'Failed to start conversation', 'error')
    } finally {
      loading.value = false
    }
  }

  /** A row is disabled when picking it would form a group with somebody
   *  whose household can't take part in group chats yet: the row is such
   *  a person and someone is already picked, or such a person is already
   *  picked. Already-picked rows are always enabled (so the user can
   *  un-pick). */
  function isRowDisabled(row: Pickable): boolean {
    if (picked.value.has(row.user_id)) return false
    const picks = pickedUsers.value
    if (picks.length === 0) return false
    return !row.supports_group || hasGroupBlockerPicked.value
  }

  const startDisabled = loading.value || picked.value.size === 0
  /** A group holding someone who can't join groups — unreachable through
   *  ``isRowDisabled``, kept so a future bug can't send a doomed request. */
  const groupBlocked = isGroup.value && hasGroupBlockerPicked.value

  return (
    <Modal
      open={open.value}
      onClose={() => { open.value = false; reset() }}
      title={isGroup.value ? 'New group message' : 'New message'}
    >
      <div class="sh-form sh-newdm">
        {/* Selected-chip strip — only renders when at least one is
            picked, so the dialog stays compact for quick 1:1s. */}
        {picked.value.size > 0 && (
          <div class="sh-newdm-chips" aria-label="Selected recipients">
            {pickedUsers.value.map(u => (
              <button
                key={u.user_id}
                type="button"
                class="sh-newdm-chip"
                aria-label={`Remove ${u.display_name}`}
                onClick={() => togglePick(u.user_id)}
              >
                <Avatar name={u.display_name} src={u.picture_url} size={20} />
                <span class="sh-newdm-chip-name">{u.display_name}</span>
                {u.household_name && (
                  <span
                    class="sh-newdm-chip-household sh-muted"
                    aria-label={`at ${u.household_name}`}
                  >
                    · {u.household_name}
                  </span>
                )}
                <span class="sh-newdm-chip-x" aria-hidden="true">×</span>
              </button>
            ))}
          </div>
        )}

        {/* Group-name field appears only when the conversation will
            actually be a group. Optional — backend allows null. */}
        {isGroup.value && (
          <label class="sh-newdm-name">
            Group name <span class="sh-muted">(optional)</span>
            <input
              type="text"
              maxLength={80}
              value={groupName.value}
              onInput={(e) => {
                groupName.value = (e.target as HTMLInputElement).value
              }}
              placeholder="e.g. Sunday lunch crew"
            />
          </label>
        )}

        <label class="sh-newdm-search">
          {picked.value.size === 0 ? 'To:' : 'Add more:'}
          <input
            type="search"
            placeholder="Search people…"
            value={search.value}
            onInput={(e) => {
              search.value = (e.target as HTMLInputElement).value
            }}
            autoFocus
          />
        </label>

        <div class="sh-newdm-userlist" role="listbox" aria-multiselectable="true">
          {filtered.value.length === 0 && (
            <p class="sh-muted" style={{ padding: 'var(--sh-space-sm)' }}>
              {search.value
                ? 'No matches.'
                : 'No people available yet — pair a household to start.'}
            </p>
          )}
          {filtered.value.map(u => {
            const checked = picked.value.has(u.user_id)
            const disabled = isRowDisabled(u)
            return (
              <button
                key={u.user_id}
                type="button"
                role="option"
                aria-selected={checked}
                aria-disabled={disabled}
                disabled={disabled}
                title={disabled ? disabledReason(u) : undefined}
                class={
                  checked
                    ? 'sh-newdm-row sh-newdm-row--checked'
                    : disabled
                      ? 'sh-newdm-row sh-newdm-row--disabled'
                      : 'sh-newdm-row'
                }
                onClick={() => { if (!disabled) togglePick(u.user_id) }}
              >
                <Avatar
                  name={u.display_name}
                  src={u.picture_url}
                  size={32}
                />
                <div class="sh-newdm-row-meta">
                  <strong>{u.display_name}</strong>
                  <span class="sh-muted">
                    {u.household_name
                      ? `at ${u.household_name}`
                      : `@${u.username}`}
                  </span>
                  {disabled && (
                    <span class="sh-newdm-row-note">{disabledReason(u)}</span>
                  )}
                </div>
                <span class="sh-newdm-check" aria-hidden="true">
                  {checked ? '✓' : ''}
                </span>
              </button>
            )
          })}
        </div>

        <div class="sh-form-actions">
          <Button
            variant="secondary"
            onClick={() => { open.value = false; reset() }}
          >
            Cancel
          </Button>
          <Button
            onClick={handleCreate}
            loading={loading.value}
            disabled={startDisabled || groupBlocked}
          >
            {isGroup.value
              ? `Start group (${picked.value.size})`
              : 'Start'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
