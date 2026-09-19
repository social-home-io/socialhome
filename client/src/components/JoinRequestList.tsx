/**
 * JoinRequestList — approve/deny join requests for a space (spec §23.99).
 *
 * Fetches ``GET /api/spaces/{space_id}/join-requests`` (admin/owner
 * only) and offers inline Approve/Deny buttons that hit the matching
 * POST routes. Listens for ``space.join.requested`` /
 * ``space.join.approved`` / ``space.join.denied`` WS events so the
 * list stays live when another admin acts elsewhere.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Avatar } from './Avatar'
import { Button } from './Button'
import { showToast } from './Toast'
import {
  householdDisplayName,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { relativeDocsTime } from '@/utils/relativeTime'

interface JoinRequest {
  id:            string
  space_id:      string
  user_id:       string
  message?:      string | null
  requested_at:  string
  status?:       string
  /** Server-resolved applicant name — set for a remote applicant whose
   *  id isn't in the local roster (#698); may be null. */
  display_name?: string | null
  /** Non-null ('admin') when the row is a pending role elevation: the
   *  applicant is already a member (an admin/mod invite link seated them)
   *  and is waiting for the owner to approve the admin role. */
  requested_role?: string | null
}

const requestsBySpace = signal<Record<string, JoinRequest[]>>({})

async function loadRequests(spaceId: string): Promise<void> {
  try {
    const rows = await api.get(
      `/api/spaces/${spaceId}/join-requests`,
    ) as JoinRequest[]
    requestsBySpace.value = {
      ...requestsBySpace.value,
      [spaceId]: rows,
    }
  } catch {
    // 403 for non-admins is expected — just leave an empty list so
    // the component renders nothing.
    requestsBySpace.value = { ...requestsBySpace.value, [spaceId]: [] }
  }
}

export function JoinRequestList({ spaceId }: { spaceId: string }) {
  useEffect(() => {
    // Load the household user roster so each request renders the
    // applicant's display name + avatar instead of the raw user_id
    // hash that the API returns on the wire.
    void loadHouseholdUsers()
    void loadRequests(spaceId)
    const handlers = [
      ws.on('space.join.requested', (evt) => {
        const d = evt.data as { space_id?: string }
        if (d.space_id === spaceId) void loadRequests(spaceId)
      }),
      ws.on('space.join.approved', (evt) => {
        const d = evt.data as { space_id?: string }
        if (d.space_id === spaceId) void loadRequests(spaceId)
      }),
      ws.on('space.join.denied', (evt) => {
        const d = evt.data as { space_id?: string }
        if (d.space_id === spaceId) void loadRequests(spaceId)
      }),
    ]
    return () => { handlers.forEach(off => off()) }
  }, [spaceId])

  const rows = requestsBySpace.value[spaceId] ?? []

  const act = async (request: JoinRequest, action: 'approve' | 'deny') => {
    try {
      await api.post(
        `/api/spaces/${spaceId}/join-requests/${request.id}/${action}`, {},
      )
      showToast(
        action === 'approve' ? 'Request approved' : 'Request denied',
        action === 'approve' ? 'success' : 'info',
      )
      // Optimistic drop — the WS listener refreshes either way.
      requestsBySpace.value = {
        ...requestsBySpace.value,
        [spaceId]: rows.filter(r => r.id !== request.id),
      }
    } catch (e: unknown) {
      showToast((e as Error).message || 'Action failed', 'error')
    }
  }

  if (rows.length === 0) return null

  return (
    <div class="sh-join-requests sh-card" aria-label="Pending join requests">
      <h4>Join requests ({rows.length})</h4>
      {rows.map(r => {
        const name = r.display_name || householdDisplayName(r.user_id)
        return (
          <div key={r.id} class="sh-join-request">
            <Avatar name={name} size={32} />
            <div class="sh-join-info">
              <strong>{name}</strong>
              {r.requested_role === 'admin' ? (
                <p class="sh-join-elevation">
                  Already a member — wants the <strong>admin</strong> role
                </p>
              ) : (
                r.message && <p class="sh-muted">“{r.message}”</p>
              )}
              {r.requested_at && (
                <time
                  class="sh-muted"
                  dateTime={r.requested_at}
                  title={new Date(r.requested_at).toLocaleString()}
                >
                  Asked {relativeDocsTime(r.requested_at)}
                </time>
              )}
            </div>
            <div class="sh-join-actions">
              <Button onClick={() => act(r, 'approve')}>
                {r.requested_role === 'admin' ? 'Make admin' : 'Approve'}
              </Button>
              <Button variant="secondary" onClick={() => act(r, 'deny')}>
                Decline
              </Button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
