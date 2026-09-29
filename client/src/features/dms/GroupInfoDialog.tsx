/**
 * GroupInfoDialog — who is in a group chat, and managing it (§23.47, v_37).
 *
 * Groups can span households. The household that created a group keeps
 * its member list (``managed_here`` on ``GET /api/conversations``): only
 * people there see Rename / Add people / Remove. Everyone can leave.
 *
 *   • Members list — each person with their household ("at Brother's
 *     house"; "Home" for this one), so a cross-household group reads
 *     unambiguously when two people share a first name.
 *   • Add people — built from ``GET /api/friends`` like the new-chat
 *     picker; people already in the group are left out, and people whose
 *     household is too old for group chats are greyed out with the reason
 *     on the row (touch devices have no hover tooltip).
 *   • Remove / Leave — both confirm first; a removed person can be added
 *     back, a left group can't be rejoined from here.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import {
  flattenFriends,
  type FriendsResponse,
  type Pickable,
} from '@/components/NewDmDialog'
import { MuteSection } from './ConversationMute'

/** The slice of a ``GET /api/conversations/{id}/members`` row this uses. */
export interface GroupMember {
  user_id: string
  username: string
  display_name: string
  picture_url: string | null
  is_self: boolean
  instance_id?: string | null
  household_name?: string | null
}

interface Props {
  open: boolean
  convId: string
  name: string | null
  managedHere: boolean
  /** The viewer's own mute (``null`` = not muted) and its setter. */
  mutedUntil: string | null
  onMuteChange: (mutedUntil: string | null) => void
  members: GroupMember[]
  onClose: () => void
  /** The roster / name changed — the thread refetches it. */
  onChanged: () => void
  /** The viewer left — the thread navigates away. */
  onLeft: () => void
}

const nameDraft = signal<string | null>(null)
const adding = signal(false)
const candidates = signal<Pickable[] | null>(null)
const candidatesError = signal<string | null>(null)
const picked = signal<Set<string>>(new Set())
const busy = signal(false)
const confirmRemove = signal<GroupMember | null>(null)
const confirmLeave = signal(false)

function resetState() {
  nameDraft.value = null
  adding.value = false
  candidates.value = null
  candidatesError.value = null
  picked.value = new Set()
  confirmRemove.value = null
  confirmLeave.value = false
}

function householdLabel(m: GroupMember): string {
  if (!m.instance_id) return 'Home'
  return m.household_name ? `at ${m.household_name}` : 'at another household'
}

async function loadCandidates() {
  candidatesError.value = null
  try {
    const payload = await api.get('/api/friends') as FriendsResponse
    candidates.value = flattenFriends(payload)
  } catch {
    candidates.value = []
    candidatesError.value = 'Couldn’t load people — check your connection and try again.'
  }
}

export function GroupInfoDialog({
  open, convId, name, managedHere, mutedUntil, onMuteChange, members, onClose,
  onChanged, onLeft,
}: Props) {
  // Every opening starts clean — no half-picked list or pending confirm
  // left over from another group (the state is module-level).
  useEffect(() => { if (open) resetState() }, [open, convId])
  const close = () => { resetState(); onClose() }
  const inGroup = new Set(members.map(m => m.user_id))
  const addable = (candidates.value ?? []).filter(c => !inGroup.has(c.user_id))
  const draft = nameDraft.value ?? name ?? ''

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    if (busy.value) return
    busy.value = true
    try {
      await fn()
      showToast(ok, 'success')
      onChanged()
      return true
    } catch (e: any) {
      showToast(e?.message || 'That didn’t work — try again.', 'error')
      return false
    } finally {
      busy.value = false
    }
  }

  const saveName = async () => {
    const trimmed = draft.trim()
    if (trimmed === (name ?? '')) return
    const done = await run(
      () => api.patch(`/api/conversations/${convId}`, { name: trimmed || null }),
      trimmed ? `Renamed to "${trimmed}"` : 'Group name cleared',
    )
    if (done) nameDraft.value = null
  }

  const addPicked = async () => {
    const picks = addable.filter(c => picked.value.has(c.user_id))
    if (picks.length === 0) return
    const done = await run(
      () => api.post(`/api/conversations/${convId}/members`, {
        usernames: picks.filter(p => p.instance_id === null).map(p => p.username),
        user_ids: picks.filter(p => p.instance_id !== null).map(p => p.user_id),
      }),
      picks.length === 1 ? `${picks[0].display_name} added` : `${picks.length} people added`,
    )
    if (done) {
      adding.value = false
      picked.value = new Set()
    }
  }

  const remove = async (m: GroupMember) => {
    confirmRemove.value = null
    await run(
      () => api.delete(`/api/conversations/${convId}/members/${encodeURIComponent(m.user_id)}`),
      `${m.display_name} removed`,
    )
  }

  const leave = async () => {
    confirmLeave.value = false
    if (busy.value) return
    busy.value = true
    try {
      await api.post(`/api/conversations/${convId}/leave`)
      showToast('You left the group', 'success')
      resetState()
      onLeft()
    } catch (e: any) {
      showToast(e?.message || 'Couldn’t leave the group — try again.', 'error')
    } finally {
      busy.value = false
    }
  }

  const toggle = (id: string) => {
    const next = new Set(picked.value)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    picked.value = next
  }

  return (
    <>
      <Modal open={open} onClose={close} title="Group info">
        <div class="sh-form sh-groupinfo">
          {managedHere ? (
            <form
              class="sh-groupinfo-name"
              onSubmit={(e) => { e.preventDefault(); void saveName() }}
            >
              <label>
                Group name <span class="sh-muted">(optional)</span>
                <input
                  type="text"
                  maxLength={80}
                  value={draft}
                  placeholder="e.g. Sunday lunch crew"
                  onInput={(e) => { nameDraft.value = (e.target as HTMLInputElement).value }}
                />
              </label>
              <Button
                type="submit"
                variant="secondary"
                disabled={busy.value || draft.trim() === (name ?? '')}
              >
                Save
              </Button>
            </form>
          ) : (
            <p class="sh-muted sh-groupinfo-note">
              {name ? <strong class="sh-groupinfo-title">{name}</strong> : null}
              Only people in the household that started this group can
              rename it or change who is in it. You can leave at any time.
            </p>
          )}

          <h3 class="sh-groupinfo-heading">{members.length} people</h3>
          <ul class="sh-groupinfo-members">
            {members.map(m => (
              <li key={m.user_id} class="sh-groupinfo-member">
                <Avatar name={m.display_name} src={m.picture_url} size={32} />
                <div class="sh-newdm-row-meta">
                  <strong>{m.display_name}{m.is_self ? ' (you)' : ''}</strong>
                  <span class="sh-muted">{householdLabel(m)}</span>
                </div>
                {managedHere && !m.is_self && (
                  <Button
                    variant="ghost"
                    disabled={busy.value}
                    aria-label={`Remove ${m.display_name} from the group`}
                    onClick={() => { confirmRemove.value = m }}
                  >
                    Remove
                  </Button>
                )}
              </li>
            ))}
          </ul>

          {managedHere && !adding.value && (
            <Button
              variant="secondary"
              onClick={() => { adding.value = true; void loadCandidates() }}
            >
              Add people
            </Button>
          )}

          {managedHere && adding.value && (
            <div class="sh-groupinfo-add">
              <h3 class="sh-groupinfo-heading">Add people</h3>
              {candidates.value === null && <p class="sh-muted">Loading…</p>}
              {candidatesError.value && (
                <p class="sh-muted" role="alert">{candidatesError.value}</p>
              )}
              {candidates.value !== null && !candidatesError.value && addable.length === 0 && (
                <p class="sh-muted">
                  Everyone you can reach is already in this group — pair
                  another household to add its people.
                </p>
              )}
              {addable.length > 0 && (
                <div class="sh-newdm-userlist" role="listbox" aria-multiselectable="true">
                  {addable.map(c => {
                    const checked = picked.value.has(c.user_id)
                    const disabled = !c.supports_group
                    const note = `${c.household_name ?? 'Their household'} needs a Social Home update for group chats`
                    return (
                      <button
                        key={c.user_id}
                        type="button"
                        role="option"
                        aria-selected={checked}
                        aria-disabled={disabled}
                        disabled={disabled}
                        title={disabled ? note : undefined}
                        class={
                          checked
                            ? 'sh-newdm-row sh-newdm-row--checked'
                            : disabled
                              ? 'sh-newdm-row sh-newdm-row--disabled'
                              : 'sh-newdm-row'
                        }
                        onClick={() => { if (!disabled) toggle(c.user_id) }}
                      >
                        <Avatar name={c.display_name} src={c.picture_url} size={32} />
                        <div class="sh-newdm-row-meta">
                          <strong>{c.display_name}</strong>
                          <span class="sh-muted">
                            {c.household_name ? `at ${c.household_name}` : `@${c.username}`}
                          </span>
                          {disabled && <span class="sh-newdm-row-note">{note}</span>}
                        </div>
                        <span class="sh-newdm-check" aria-hidden="true">{checked ? '✓' : ''}</span>
                      </button>
                    )
                  })}
                </div>
              )}
              <div class="sh-form-actions">
                <Button
                  variant="secondary"
                  onClick={() => { adding.value = false; picked.value = new Set() }}
                >
                  Cancel
                </Button>
                <Button
                  onClick={() => { void addPicked() }}
                  loading={busy.value}
                  disabled={picked.value.size === 0}
                >
                  {picked.value.size > 0 ? `Add (${picked.value.size})` : 'Add'}
                </Button>
              </div>
            </div>
          )}

          {!adding.value && (
            <MuteSection convId={convId} mutedUntil={mutedUntil} onChange={onMuteChange} />
          )}

          {!adding.value && (
          <div class="sh-groupinfo-leave">
            <Button
              variant="danger"
              disabled={busy.value}
              onClick={() => { confirmLeave.value = true }}
            >
              Leave group
            </Button>
          </div>
          )}
        </div>
      </Modal>
      <ConfirmDialog
        open={confirmRemove.value !== null}
        title="Remove from group?"
        message={
          confirmRemove.value
            ? `${confirmRemove.value.display_name} won't get new messages in this group. You can add them back later.`
            : ''
        }
        confirmLabel="Remove"
        destructive
        onConfirm={() => { const m = confirmRemove.value; if (m) void remove(m) }}
        onCancel={() => { confirmRemove.value = null }}
      />
      <ConfirmDialog
        open={confirmLeave.value}
        title="Leave this group?"
        message="You'll stop getting its messages. To come back, someone in the group has to add you again."
        confirmLabel="Leave"
        destructive
        onConfirm={() => { void leave() }}
        onCancel={() => { confirmLeave.value = false }}
      />
    </>
  )
}
