/**
 * UserActionsMenu — overflow menu for user-targeted actions (§Privacy).
 *
 * v1 surfaces a single action: **Block** the user. The menu is opened
 * via :func:`openUserActions` and rendered once in the app shell next
 * to the other global dialogs.
 *
 * Once a block lands the global :func:`blockedUserIds` signal updates,
 * so any list / ring that reads that signal hides the row immediately
 * — the server's repo-layer filter is already authoritative; this is
 * just the optimistic UI sync. The blocker is offered an Unblock
 * shortcut from Settings → Privacy → Blocked accounts.
 */
import { signal } from '@preact/signals'
import { Modal } from './Modal'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { showToast } from './Toast'
import { openReport } from './ReportDialog'
import { blockUser, unblockUser, isBlocked } from '@/store/blocks'
import {
  followUser,
  unfollowUser,
  isFollowing,
  loadFollows,
} from '@/store/follows'
import { t } from '@/i18n/i18n'

const open = signal(false)
const targetUserId = signal('')
const targetDisplayName = signal('')
const showConfirmBlock = signal(false)

/** Open the menu against ``user_id`` / ``display_name``.  ``display_name``
 *  is rendered in the confirm body — fall back to the user_id if you
 *  don't have a friendlier label handy. */
export function openUserActions(userId: string, displayName?: string): void {
  targetUserId.value = userId
  targetDisplayName.value = displayName?.trim() || userId
  showConfirmBlock.value = false
  open.value = true
  // Hydrate the follow cache so the Follow / Unfollow toggle reflects
  // the current state when the modal opens.
  void loadFollows()
}

export function UserActionsMenu() {
  const alreadyBlocked = isBlocked(targetUserId.value)
  const alreadyFollowing = isFollowing(targetUserId.value)

  const onBlock = async () => {
    try {
      await blockUser(targetUserId.value)
      showToast(t('user_actions.blocked', { name: targetDisplayName.value }), 'success')
      showConfirmBlock.value = false
      open.value = false
    } catch (e: unknown) {
      showToast(t('user_actions.block_failed', { error: String((e as Error)?.message ?? e) }), 'error')
    }
  }

  const onUnblock = async () => {
    try {
      await unblockUser(targetUserId.value)
      showToast(t('user_actions.unblocked', { name: targetDisplayName.value }), 'success')
      open.value = false
    } catch (e: unknown) {
      showToast(t('user_actions.unblock_failed', { error: String((e as Error)?.message ?? e) }), 'error')
    }
  }

  const onFollow = async () => {
    try {
      await followUser(targetUserId.value)
      showToast(t('user_actions.followed', { name: targetDisplayName.value }), 'success')
      open.value = false
    } catch (e: unknown) {
      showToast(t('user_actions.follow_failed', { error: String((e as Error)?.message ?? e) }), 'error')
    }
  }

  const onUnfollow = async () => {
    try {
      await unfollowUser(targetUserId.value)
      showToast(t('user_actions.unfollowed', { name: targetDisplayName.value }), 'success')
      open.value = false
    } catch (e: unknown) {
      showToast(t('user_actions.unfollow_failed', { error: String((e as Error)?.message ?? e) }), 'error')
    }
  }

  return (
    <>
      <Modal
        open={open.value}
        onClose={() => { open.value = false }}
        title={targetDisplayName.value}
      >
        <div class="sh-user-actions">
          {!alreadyBlocked && !alreadyFollowing && (
            <Button variant="primary" onClick={onFollow}>
              ➕ {t('user_actions.follow')}
            </Button>
          )}
          {!alreadyBlocked && alreadyFollowing && (
            <Button variant="secondary" onClick={onUnfollow}>
              ✓ {t('user_actions.unfollow')}
            </Button>
          )}
          {/* Report sits between Follow and Block — it's the
           *  middle-ground response when something a user posts is
           *  off but you don't want to sever the connection.  Wired
           *  to ``ReportDialog`` with type=user so the moderation
           *  flow uses the same surface as post reports. */}
          {!alreadyBlocked && (
            <Button
              variant="secondary"
              onClick={() => {
                open.value = false
                openReport('user', targetUserId.value)
              }}
            >
              🚩 {t('user_actions.report')}
            </Button>
          )}
          {!alreadyBlocked && (
            <Button
              variant="danger"
              onClick={() => { showConfirmBlock.value = true }}
            >
              🚫 {t('user_actions.block')}
            </Button>
          )}
          {alreadyBlocked && (
            <Button variant="secondary" onClick={onUnblock}>
              ✓ {t('user_actions.unblock')}
            </Button>
          )}
          <Button variant="ghost" onClick={() => { open.value = false }}>
            {t('common.cancel')}
          </Button>
        </div>
      </Modal>
      <ConfirmDialog
        open={showConfirmBlock.value}
        title={t('user_actions.block_title', { name: targetDisplayName.value })}
        message={t('user_actions.block_body')}
        confirmLabel={t('user_actions.block_confirm')}
        destructive
        onConfirm={onBlock}
        onCancel={() => { showConfirmBlock.value = false }}
      />
    </>
  )
}
