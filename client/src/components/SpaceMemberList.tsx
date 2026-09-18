/**
 * SpaceMemberList — member list view (§23.28).
 *
 * Also surfaces the banned-members list and a kebab menu that opens
 * :func:`openMemberActions` from :mod:`./MemberActionSheet`. Admins see
 * role changes, ban, and unban actions. Regular members see a read-only
 * list.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Avatar } from './Avatar'
import { Spinner } from './Spinner'
import { Button } from './Button'
import { showToast } from './Toast'
import { AliasDialog, openAliasDialog } from './AliasDialog'
import { openMemberActions, MemberActionSheet } from './MemberActionSheet'
import {
  SpaceProfileDialog,
  openSpaceProfileDialog,
} from './SpaceProfileDialog'
import { openSpaceInvite } from './SpaceInviteDialog'
import { openRemoteInviteDialog } from './RemoteInviteDialog'
import { currentUser } from '@/store/auth'
import { resolveAvatar } from '@/utils/avatar'

/** Friendly relative duration for "joined …" — drops the
 *  utilitarian ``5/8/2026`` for "joined today" / "joined 3 days ago"
 *  / "joined last month" / "joined Apr 2026". Quick formatter; the
 *  shared :mod:`@/utils/calendar` helper covers per-day labels but
 *  member-joined wants weeks/months/years too. */
function formatRelativeJoined(iso: string): string {
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return iso
  const now = new Date()
  const days = Math.floor((now.getTime() - then.getTime()) / 86400000)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 7) return `${days} days ago`
  if (days < 14) return 'last week'
  if (days < 30) return `${Math.floor(days / 7)} weeks ago`
  if (days < 60) return 'last month'
  if (days < 365) return `${Math.floor(days / 30)} months ago`
  // Older than a year — show the month + year so "joined Apr 2024"
  // reads cleaner than "joined 19 months ago".
  return then.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
}

interface Member {
  user_id: string
  display_name?: string | null
  role: string
  joined_at: string
  space_display_name?: string | null
  /** §4.1.6 — viewer's private rename (only the requesting user sees it). */
  personal_alias?: string | null
  picture_url?: string | null
  picture_hash?: string | null
  /** Session presence (§ online status). Patched live by user.online /
   *  user.idle / user.offline WS frames. */
  is_online?: boolean
  is_idle?: boolean
  last_seen_at?: string | null
  /** §D1b — set when this row represents a federated peer who joined
   *  via the cross-household invite flow. Drives the "from another
   *  household" badge + suppresses admin-only gestures (kebab menu,
   *  rename) that don't apply over federation. */
  instance_id?: string | null
}

interface Ban {
  user_id: string
  banned_by: string
  banned_at: string
  reason?: string | null
}

const members = signal<Member[]>([])
const bans = signal<Ban[]>([])
const loading = signal(true)
const canManage = signal(false)

interface Props {
  spaceId: string
  /** Viewer's role in this space — drives whether admin actions render.
   *  ``'subscriber'`` appears when the caller is a read-only subscriber
   *  (see :class:`SpaceService.subscribe_to_space`); no admin actions
   *  render for that role. */
  viewerRole?: 'owner' | 'admin' | 'member' | 'subscriber'
}

export function SpaceMemberList({ spaceId, viewerRole }: Props) {
  const reload = () => {
    loading.value = true
    const p1 = api.get(`/api/spaces/${spaceId}/members`)
    const p2 = viewerRole === 'owner' || viewerRole === 'admin'
      ? api.get(`/api/spaces/${spaceId}/bans`).catch(() => [] as Ban[])
      : Promise.resolve([] as Ban[])
    Promise.all([p1, p2]).then(([mems, bansList]) => {
      members.value = mems as Member[]
      bans.value = bansList as Ban[]
      canManage.value = viewerRole === 'owner' || viewerRole === 'admin'
      loading.value = false
    })
  }

  useEffect(() => {
    reload()
    // Live patch session-presence on the visible roster — saves a full
    // /api/spaces/{id}/members refetch on every connect / disconnect.
    const patch = (
      user_id: string,
      next: { is_online: boolean; is_idle: boolean; last_seen_at?: string | null },
    ) => {
      members.value = members.value.map(m =>
        m.user_id === user_id
          ? {
              ...m,
              is_online: next.is_online,
              is_idle: next.is_idle,
              ...(next.last_seen_at !== undefined ? { last_seen_at: next.last_seen_at } : {}),
            }
          : m,
      )
    }
    const offOnline = ws.on('user.online', (e) => {
      const d = e.data as { user_id?: string }
      if (d.user_id) patch(d.user_id, { is_online: true, is_idle: false })
    })
    const offIdle = ws.on('user.idle', (e) => {
      const d = e.data as { user_id?: string }
      if (d.user_id) patch(d.user_id, { is_online: true, is_idle: true })
    })
    const offOffline = ws.on('user.offline', (e) => {
      const d = e.data as { user_id?: string; last_seen_at?: string | null }
      if (d.user_id) patch(d.user_id, {
        is_online: false,
        is_idle: false,
        last_seen_at: d.last_seen_at ?? null,
      })
    })
    // Roster mutations — the backend broadcasts join / leave / profile
    // change to every space member (frame carries ``space_id``). The
    // join frame is too thin to build a row from, so refetch: a full
    // ``/members`` reload is the simplest correct path and the list is
    // small. Scope each frame to THIS space so an unrelated space's
    // churn doesn't reload our roster.
    const onRoster = (e: { data: { space_id?: string } }) => {
      if (e.data.space_id === spaceId) reload()
    }
    const offJoined = ws.on('space.member.joined', onRoster)
    const offLeft = ws.on('space.member.left', onRoster)
    const offProfile = ws.on('space.member.profile_updated', onRoster)
    return () => {
      offOnline(); offIdle(); offOffline()
      offJoined(); offLeft(); offProfile()
    }
  }, [spaceId, viewerRole])

  if (loading.value) return <Spinner />

  const roleBadge = (role: string) => {
    if (role === 'owner') return <span class="sh-badge sh-badge--owner">Owner</span>
    if (role === 'admin') return <span class="sh-badge sh-badge--admin">Admin</span>
    return null
  }

  const unban = async (userId: string) => {
    try {
      await api.delete(`/api/spaces/${spaceId}/bans/${userId}`)
      showToast('Member unbanned', 'success')
      reload()
    } catch (e: any) {
      showToast(e.message || 'Unban failed', 'error')
    }
  }

  const me = currentUser.value?.user_id
  const myRow = me ? members.value.find(m => m.user_id === me) : null
  const myDisplayName = myRow?.space_display_name
    || myRow?.display_name
    || currentUser.value?.display_name
    || '—'
  const myPicture = myRow?.picture_url
    ?? currentUser.value?.picture_url
    ?? null
  const hasOverride = !!(myRow?.space_display_name || myRow?.picture_url)

  // §4.1.6 resolution priority — space_display_name > personal_alias > display_name.
  // Returns the canonical name to show plus a flag for the "✏ nickname" chip.
  const resolveName = (m: Member) => {
    const fallback = m.display_name || m.user_id
    if (m.space_display_name) {
      return { name: m.space_display_name, source: 'space' as const, fallback }
    }
    if (m.personal_alias) {
      return { name: m.personal_alias, source: 'personal' as const, fallback }
    }
    return { name: fallback, source: 'global' as const, fallback }
  }

  return (
    <>
      <div class="sh-member-list">
        {me && (
          <div class="sh-member-me-card">
            <Avatar name={myDisplayName} src={myPicture} size={56} />
            <div class="sh-member-me-card-body">
              <div class="sh-row" style={{ gap: 'var(--sh-space-xs)', alignItems: 'baseline' }}>
                <strong>{myDisplayName}</strong>
                <span class="sh-member-me-chip">you</span>
              </div>
              <span class="sh-muted"
                    style={{ fontSize: 'var(--sh-font-size-xs)' }}>
                {hasOverride
                  ? '✨ Custom profile for this space'
                  : '⬇ Using your household profile'}
              </span>
            </div>
            <Button variant="secondary"
                    onClick={() => openSpaceProfileDialog(spaceId)}>
              Edit
            </Button>
          </div>
        )}
        <div class="sh-member-list-header">
          <h3>{members.value.length} members</h3>
          {canManage.value && (
            <div class="sh-member-list-invite-actions">
              <Button onClick={() => openSpaceInvite(spaceId, null, viewerRole)}>
                + Invite by code
              </Button>
              <Button
                variant="secondary"
                onClick={() => openRemoteInviteDialog(spaceId)}
              >
                + Add person
              </Button>
            </div>
          )}
        </div>
        {members.value.map(m => {
          const r = resolveName(m)
          const isMe = m.user_id === me
          return (
            <div
              key={m.user_id}
              class={[
                'sh-member-row',
                `sh-member-row--${m.role}`,
                isMe ? 'sh-member-row--me' : '',
              ].filter(Boolean).join(' ')}
            >
              <Avatar
                name={r.name}
                // ``m.picture_url`` is the per-space override only —
                // local members typically don't set one, so the row
                // would show initials even when the household-level
                // avatar is cached. Route through ``resolveAvatar``
                // so the householdUsers fallback (and, for remote
                // members, the picture URL backend serves directly)
                // kicks in. The space-override still wins when set.
                src={resolveAvatar(spaceId, m.user_id, null)}
                size={32}
                online={m.is_online ? (m.is_idle ? 'idle' : 'online') : null}
              />
              <div class="sh-member-info">
                <span class="sh-member-name">{r.name}</span>
                {r.source === 'space' && r.name !== m.display_name && (
                  <span class="sh-member-original-name">
                    (household: {m.display_name || m.user_id})
                  </span>
                )}
                {r.source === 'personal' && (
                  <span class="sh-member-alias-chip"
                        title={`Originally: ${r.fallback}`}>
                    ✏ Your nickname
                  </span>
                )}
                {roleBadge(m.role)}
                {m.instance_id && (
                  <span
                    class="sh-member-remote-chip"
                    title="Joined from another household via federation"
                  >
                    🏘 Other household
                  </span>
                )}
                {isMe && (
                  <span class="sh-muted sh-member-me-chip">you</span>
                )}
                {/* Joined date moved INSIDE the column-flex info block
                    so it stacks underneath the name + chips instead of
                    sharing a row with them. For a long display name on
                    a same-household row the two were colliding (no room
                    for both "Pascal Vizeli ✏ Your nickname" and the
                    date on a 360 px phone). On its own line the date
                    reads as quiet metadata. */}
                <time
                  class="sh-muted sh-member-joined"
                  dateTime={m.joined_at}
                  title={new Date(m.joined_at).toLocaleString()}
                >
                  joined {formatRelativeJoined(m.joined_at)}
                </time>
              </div>
              {!isMe && (
                <button
                  class="sh-member-rename-btn"
                  type="button"
                  aria-label={
                    m.personal_alias
                      ? `Edit nickname for ${r.fallback}`
                      : `Set nickname for ${r.fallback}`
                  }
                  // Viewer-private nickname applies to local and
                  // remote members alike — ``personal_aliases`` is
                  // keyed on ``(viewer, target_user_id)`` regardless
                  // of which household ``target`` lives on. The
                  // backend's ``GET /api/spaces/{id}/members`` now
                  // surfaces the alias for remote rows too.
                  title="Set a nickname (only you see it)"
                  onClick={() =>
                    openAliasDialog({
                      targetUserId: m.user_id,
                      globalDisplayName: m.display_name || m.user_id,
                      currentAlias: m.personal_alias ?? null,
                      onSave: (newAlias) => {
                        // Optimistic update — patch the row in place so
                        // the new name shows before the next reload tick.
                        members.value = members.value.map(row =>
                          row.user_id === m.user_id
                            ? { ...row, personal_alias: newAlias }
                            : row,
                        )
                      },
                    })
                  }
                >
                  ✏
                </button>
              )}
              {canManage.value && !isMe && (
                <button
                  class="sh-post-overflow"
                  type="button"
                  aria-label={`Manage ${r.name}`}
                  onClick={() => openMemberActions(
                    spaceId, m.user_id, m.role, m.instance_id ?? null,
                  )}
                >
                  ···
                </button>
              )}
            </div>
          )
        })}
        {canManage.value && bans.value.length > 0 && (
          <>
            <h3>Banned</h3>
            {bans.value.map(b => (
              <div key={b.user_id} class="sh-member-row sh-member-row--banned">
                <Avatar name={b.user_id} size={32} />
                <div class="sh-member-info">
                  <span class="sh-member-name">{b.user_id}</span>
                  <span class="sh-badge sh-badge--danger">Banned</span>
                  {b.reason && (
                    <span class="sh-muted">{b.reason}</span>
                  )}
                </div>
                <Button
                  variant="secondary"
                  onClick={() => unban(b.user_id)}
                >
                  Unban
                </Button>
              </div>
            ))}
          </>
        )}
      </div>
      <MemberActionSheet onUpdate={reload} />
      <SpaceProfileDialog />
      <AliasDialog />
    </>
  )
}
