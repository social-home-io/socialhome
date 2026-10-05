/**
 * MemberActionSheet — role/ban actions on space members (§23.98).
 *
 * The role picker offers exactly what :func:`roleChangeOptions` allows
 * the viewer (the server's ``role_change_allowed`` matrix): the owner
 * sets admin / moderator / member, an admin moves a seat only between
 * member and moderator. A refused change shows the server's reason in
 * the sheet (e.g. "household must upgrade") instead of closing it.
 *
 * On a member household (the space is hosted elsewhere) the server
 * forwards the change to the host (v_47) and answers 202
 * ``{forwarded: true}``: the sheet says "Sent to the space's host" and
 * the member list refreshes when the host's role update arrives
 * (``space.config.changed``). A change the host refuses never arrives.
 */
import { signal } from '@preact/signals'
import { api, ApiError } from '@/api'
import { parseSpaceRole, roleChangeOptions, type SpaceRole } from '@/features/spaces/spaceRoles'
import { Modal } from './Modal'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

const open = signal(false)
const memberUserId = signal('')
const memberRole = signal('')
const memberInstanceId = signal<string | null>(null)
const actorRole = signal<SpaceRole | undefined>(undefined)
const spaceId = signal('')
const showBanConfirm = signal(false)
const roleError = signal<string | null>(null)
const roleBusy = signal(false)

export function openMemberActions(
  sid: string,
  userId: string,
  role: string,
  instanceId: string | null = null,
  viewerRole?: SpaceRole,
) {
  spaceId.value = sid
  memberUserId.value = userId
  memberRole.value = role
  memberInstanceId.value = instanceId
  actorRole.value = viewerRole
  roleError.value = null
  roleBusy.value = false
  open.value = true
}

const ROLE_OPTION_KEY: Record<SpaceRole, string> = {
  owner: 'space.role.owner',
  admin: 'space.member.make_admin',
  moderator: 'space.member.make_moderator',
  member: 'space.member.make_member',
  subscriber: 'space.member.make_member',
}

/** Translated copy for a refused role change, keyed on the API code. */
function roleErrorCopy(e: unknown): string {
  const code = e instanceof ApiError ? e.code : null
  if (code === 'HOUSEHOLD_UPGRADE_REQUIRED') return t('space.member.role_upgrade_required')
  if (code === 'HOST_TOO_OLD') return t('space.member.role_host_too_old')
  if (code === 'HOST_UNREACHABLE') {
    const reason = e instanceof ApiError ? e.extra.reason : null
    return t(reason === 'unknown_host' ? 'space.host.unknown' : 'space.host.unreachable')
  }
  if (code === 'FORBIDDEN') return t('space.member.role_change_forbidden')
  return t('space.member.role_change_failed')
}

export function roleLabel(role: SpaceRole): string {
  return t(`space.role.${role}`)
}

export function MemberActionSheet({ onUpdate }: { onUpdate: () => void }) {
  const setRole = async (role: SpaceRole) => {
    roleError.value = null
    roleBusy.value = true
    try {
      const path = memberInstanceId.value
        // #114: a remote member's role lives in space_remote_members,
        // not space_members — the host-only PATCH endpoint targets the
        // ``(instance_id, user_id)`` composite key.
        ? `/api/spaces/${spaceId.value}/remote-members/${memberInstanceId.value}/${memberUserId.value}`
        : `/api/spaces/${spaceId.value}/members/${memberUserId.value}`
      const res = await api.patch<{ forwarded?: boolean } | null>(path, { role })
      if (res?.forwarded) {
        // Hosted elsewhere: the host decides; its roster update refreshes
        // the list (SpaceMemberList listens for it).
        showToast(t('space.member.role_forwarded'), 'info')
      } else {
        showToast(t('space.member.role_changed', { role: roleLabel(role) }), 'success')
      }
      open.value = false; onUpdate()
    } catch (e: unknown) {
      // Keep the sheet open with the reason — "household must upgrade"
      // is actionable, a toast that vanishes is not. Mapped by the stable
      // error code to translated copy; the server's English sentence is
      // never shown.
      roleError.value = roleErrorCopy(e)
    } finally {
      roleBusy.value = false
    }
  }
  const roleOptions = roleChangeOptions(actorRole.value, parseSpaceRole(memberRole.value))

  const ban = async () => {
    try {
      await api.post(`/api/spaces/${spaceId.value}/ban`, { user_id: memberUserId.value })
      showToast(t('member_actions.banned'), 'info')
      showBanConfirm.value = false; open.value = false; onUpdate()
    } catch (e: any) { showToast(e.message || t('member_actions.failed'), 'error') }
  }

  const remove = async () => {
    try {
      const path = memberInstanceId.value
        // #114 phase 2: a remote member kick goes to the dedicated
        // endpoint so the host can route via SPACE_REMOTE_MEMBER_REMOVED
        // + epoch rotation. Cross-household admin kicking a local
        // member from a remote space goes through the regular
        // /members/ endpoint, where SpaceService.remove_member detects
        // the remote-host case and federates SPACE_REMOTE_ADMIN_KICK.
        ? `/api/spaces/${spaceId.value}/remote-members/${memberInstanceId.value}/${memberUserId.value}`
        : `/api/spaces/${spaceId.value}/members/${memberUserId.value}`
      await api.delete(path)
      showToast(t('member_actions.removed'), 'info')
      open.value = false; onUpdate()
    } catch (e: any) { showToast(e.message || t('member_actions.failed'), 'error') }
  }

  // Ban only meaningful on the host side, and only for local members.
  // Remote members are kicked via SPACE_REMOTE_MEMBER_REMOVED instead.
  const canBan = !memberInstanceId.value

  return (
    <>
      <Modal open={open.value} onClose={() => open.value = false} title={t('member_actions.title')}>
        <div class="sh-member-actions">
          {roleOptions.length > 0 && (
            <div class="sh-member-actions-roles" role="group"
                 aria-label={t('space.member.role_heading')}>
              <span class="sh-member-actions-label">{t('space.member.role_heading')}</span>
              {roleOptions.map(r => (
                <Button
                  key={r}
                  variant="secondary"
                  data-role-option={r}
                  disabled={roleBusy.value}
                  onClick={() => setRole(r)}
                >
                  {t(ROLE_OPTION_KEY[r])}
                </Button>
              ))}
              {roleError.value && (
                <p class="sh-member-actions-error" role="alert">{roleError.value}</p>
              )}
            </div>
          )}
          <Button variant="secondary" onClick={remove}>{t('member_actions.remove')}</Button>
          {canBan && (
            <Button variant="danger" onClick={() => showBanConfirm.value = true}>{t('member_actions.ban')}</Button>
          )}
        </div>
      </Modal>
      <ConfirmDialog open={showBanConfirm.value} title={t('member_actions.ban_title')}
        message={t('member_actions.ban_body')}
        confirmLabel={t('member_actions.ban')} destructive onConfirm={ban}
        onCancel={() => showBanConfirm.value = false} />
    </>
  )
}
