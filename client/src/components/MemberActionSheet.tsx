/**
 * MemberActionSheet — role/ban actions on space members (§23.98).
 *
 * Ban removes one person; "Ban household" (a seat of another household)
 * removes everyone from that household and keeps all of its people out —
 * joining, following and invites alike — until the ban is lifted (P7b).
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
import { AdminGrantCheck } from './AdminGrantCheck'
import { Modal } from './Modal'
import { Button } from './Button'
import { BanDialog, banErrorCopy, type BanTarget } from './BanDialog'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

const open = signal(false)
const memberUserId = signal('')
const memberRole = signal('')
const memberInstanceId = signal<string | null>(null)
const actorRole = signal<SpaceRole | undefined>(undefined)
const adminsActForOwner = signal(false)
const spaceId = signal('')
const banning = signal<BanTarget | null>(null)
const householdName = signal<string | null>(null)
const memberName = signal('')
const ownerHousehold = signal(false)
const householdPeople = signal<string[]>([])
const roleError = signal<string | null>(null)
const roleBusy = signal(false)

export function openMemberActions(
  sid: string,
  userId: string,
  role: string,
  instanceId: string | null = null,
  viewerRole?: SpaceRole,
  /** The space's "Let admins act for me" switch — see AdminGrantCheck. */
  actForOwner = false,
  /** A remote seat's household name, for the household-ban confirm. */
  household: string | null = null,
  /** Who the sheet acts on (its title), whether they belong to the owner
   *  household (an admin of another household may not ban them) and the
   *  names of their household's seated people (the household confirm). */
  who: { name?: string; ownerHousehold?: boolean; people?: string[] } = {},
) {
  spaceId.value = sid
  memberUserId.value = userId
  memberRole.value = role
  memberInstanceId.value = instanceId
  actorRole.value = viewerRole
  adminsActForOwner.value = actForOwner
  householdName.value = household
  memberName.value = who.name ?? ''
  ownerHousehold.value = who.ownerHousehold === true
  householdPeople.value = who.people ?? []
  banning.value = null
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
    } catch (e: unknown) { showToast(banErrorCopy(e), 'error') }
  }

  // Anyone but the owner can be banned — a person of this household or of
  // another one (the server ends a remote seat too); never, by an admin of
  // another household, a person of the owner household (the server would
  // refuse). A remote seat also offers banning its whole household.
  const canBan = memberRole.value !== 'owner' && !ownerHousehold.value
  const canBanHousehold = canBan && !!memberInstanceId.value
  const name = memberName.value || t('member_actions.title')

  return (
    <>
      <Modal open={open.value && banning.value === null} onClose={() => open.value = false} title={name}>
        {memberInstanceId.value && householdName.value && (
          <p class="sh-muted sh-member-actions-household">
            {t('member_actions.from_household', { household: householdName.value })}
          </p>
        )}
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
              {/* Admin on offer for another household's member: who it
                  is (household ID) and what the role gives. */}
              {memberInstanceId.value && roleOptions.includes('admin') && (
                <AdminGrantCheck householdId={memberInstanceId.value}
                                 actsForSpace={adminsActForOwner.value} />
              )}
              {roleError.value && (
                <p class="sh-member-actions-error" role="alert">{roleError.value}</p>
              )}
            </div>
          )}
          <div class="sh-member-actions-leave" role="group"
               aria-label={t('member_actions.remove_or_ban')}>
            <span class="sh-member-actions-label">{t('member_actions.remove_or_ban')}</span>
            {canBan && <p class="sh-muted sh-member-actions-hint">{t('member_actions.remove_or_ban_hint')}</p>}
            <Button variant="secondary" onClick={remove}>{t('member_actions.remove')}</Button>
            {canBan && (
              <Button variant="danger" data-action="ban"
                      onClick={() => banning.value = {
                        kind: 'person', spaceId: spaceId.value,
                        userId: memberUserId.value, name: memberName.value || undefined,
                      }}>
                {t('member_actions.ban')}
              </Button>
            )}
            {canBanHousehold && (
              <Button variant="danger" data-action="ban-household"
                      onClick={() => banning.value = {
                        kind: 'household', spaceId: spaceId.value,
                        instanceId: memberInstanceId.value ?? undefined,
                        household: householdName.value, people: householdPeople.value,
                      }}>
                {t('member_actions.ban_household')}
              </Button>
            )}
          </div>
        </div>
      </Modal>
      <BanDialog target={banning.value}
                 onClose={() => banning.value = null}
                 onBanned={() => { banning.value = null; open.value = false; onUpdate() }} />
    </>
  )
}
