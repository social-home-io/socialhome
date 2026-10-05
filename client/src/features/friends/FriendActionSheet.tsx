/**
 * FriendActionSheet — the small modal that opens when the user taps
 * a friend chip on the Friends dashboard.
 *
 * Surfaces two actions side by side:
 *  - **💬 Message** — primary. Bootstraps a 1:1 DM (same path the
 *    chip used to call inline).
 *  - **✏ Edit nickname** — secondary. Opens the existing
 *    :func:`openAliasDialog` so the viewer can set / reset a
 *    viewer-private alias.
 *
 * Why a sheet rather than a hover-reveal ✏: the rename affordance
 * needs to be reachable on mobile too. Hover doesn't exist there,
 * and crowding two buttons into a small chip looked busy at 390 px.
 * One tap → sheet → two clear targets is the cleaner trade.
 */
import { signal } from '@preact/signals'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { Avatar } from '@/components/Avatar'
import { openAliasDialog } from '@/components/AliasDialog'
import { t } from '@/i18n/i18n'

/** Identifies the friend the sheet is opened for. ``username`` is
 *  required for local members (the DM-create endpoint takes
 *  ``{username}`` for local, ``{user_id}`` for remote). */
export interface FriendTarget {
  user_id: string
  username: string | null  // null for remote members
  display_name: string
  personal_alias: string | null
  picture_url: string | null
  /** Household name shown under the title — "Beta House" / "Home". */
  household: string
  is_local: boolean
}

const target = signal<FriendTarget | null>(null)
const dmBusyId = signal<string | null>(null)

/** Imperative opener — call from a chip's click handler. */
export function openFriendActions(t: FriendTarget) {
  target.value = t
}

/** Called by the FriendsPage to wire its DM-start logic in. The
 *  sheet doesn't know how to mint a conversation; the page owns
 *  that path because it already debounces in-flight DM starts via
 *  ``dmBusy`` Set. */
export interface FriendActionSheetProps {
  onStartDm: (t: FriendTarget) => Promise<void> | void
  onAliasChanged?: (user_id: string, alias: string | null) => void
}

export function FriendActionSheet(
  { onStartDm, onAliasChanged }: FriendActionSheetProps,
) {
  const ft = target.value
  if (!ft) return null

  const close = () => { target.value = null }
  const onMessage = async () => {
    dmBusyId.value = ft.user_id
    try {
      await onStartDm(ft)
      close()
    } finally {
      dmBusyId.value = null
    }
  }
  const onRename = () => {
    // Close the sheet first so the AliasDialog opens cleanly above
    // an empty z-stack — Modal stacking works either way, but
    // sequential clicks read better visually.
    const args = {
      targetUserId: ft.user_id,
      globalDisplayName: ft.display_name,
      currentAlias: ft.personal_alias,
      onSave: (newAlias: string | null) => {
        onAliasChanged?.(ft.user_id, newAlias)
      },
    }
    close()
    openAliasDialog(args)
  }

  const aliasSet = !!ft.personal_alias && ft.personal_alias !== ft.display_name
  const renameLabel = `✏ ${aliasSet ? t('friends.edit_nickname') : t('friends.set_nickname')}`

  return (
    <Modal open={true} onClose={close} title={ft.personal_alias ?? ft.display_name}>
      <div class="sh-friend-actions">
        <div class="sh-friend-actions__header">
          <Avatar
            name={ft.personal_alias ?? ft.display_name}
            src={ft.picture_url}
            size={48}
          />
          <div class="sh-friend-actions__identity">
            <strong>{ft.personal_alias ?? ft.display_name}</strong>
            {aliasSet && (
              <span class="sh-muted sh-friend-actions__real-name">
                {t('friends.their_name', { name: ft.display_name })}
              </span>
            )}
            <span class="sh-muted sh-friend-actions__household">
              {ft.household}
            </span>
          </div>
        </div>
        <div class="sh-modal-actions sh-friend-actions__buttons">
          <Button
            variant="secondary"
            onClick={onRename}
            data-testid="friend-action-rename"
          >
            {renameLabel}
          </Button>
          <Button
            onClick={onMessage}
            loading={dmBusyId.value === ft.user_id}
            data-testid="friend-action-message"
          >
            💬 {t('friends.message')}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
