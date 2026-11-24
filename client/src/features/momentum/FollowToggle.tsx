/**
 * FollowToggle — the one Follow / Following button Discover uses for both
 * kinds of follow (a friend's household follow, a GFS author's GFS follow).
 *
 * * A toggle (``aria-pressed``) labelled per person, with a check icon when
 *   on; a fixed minimum width so "Folgen" and "Folge ich" don't shift the row.
 * * Follow is one tap with a "Following {name}" toast. Unfollow is one tap
 *   too, and its toast says plainly that the moments saved from that person
 *   go (the 0124 triggers drop them with the last follow), with Undo — Undo
 *   follows again; it can't bring the saved moments back.
 *
 * The section above the list says what each kind of follow does; the button
 * looks the same for both.
 */
import { showToast } from '@/components/Toast'
import { ConnIcon } from '@/features/connections/ConnIcon'
import { t } from '@/i18n/i18n'

interface Props {
  name: string
  following: boolean
  busy: boolean
  onClick: () => void
}

export function FollowToggle({ name, following, busy, onClick }: Props) {
  return (
    // A plain <button>: ``Button`` replaces its classes when given one.
    <button
      type="button"
      class={`sh-btn sh-btn--${following ? 'secondary' : 'primary'} sh-follow-toggle`}
      aria-pressed={following ? 'true' : 'false'}
      aria-label={t(following ? 'moment.discover.following_aria' : 'moment.discover.follow_aria', { name })}
      disabled={busy}
      onClick={onClick}
    >
      {busy ? <span class="sh-spinner-sm" /> : (
        <>
          {following && <ConnIcon name="check" />}
          {following ? t('person.following') : t('user_actions.follow')}
        </>
      )}
    </button>
  )
}

/** Follow or unfollow *name* with the toasts above. ``follow`` and
 *  ``unfollow`` are the store calls for this kind of follow. */
export async function toggleFollow(
  name: string,
  following: boolean,
  follow: () => Promise<unknown>,
  unfollow: () => Promise<unknown>,
): Promise<void> {
  try {
    if (following) {
      await unfollow()
      showToast(t('moment.discover.unfollowed_undo', { name }), 'success', {
        action: {
          label: t('common.undo'),
          onClick: () => { void refollow(name, follow) },
        },
      })
    } else {
      await follow()
      showToast(t('user_actions.followed', { name }), 'success')
    }
  } catch (err) {
    showToast(
      t(following ? 'user_actions.unfollow_failed' : 'user_actions.follow_failed',
        { error: errorText(err) }),
      'error',
    )
  }
}

async function refollow(name: string, follow: () => Promise<unknown>): Promise<void> {
  try {
    await follow()
  } catch (err) {
    showToast(t('user_actions.follow_failed', { error: errorText(err) }), 'error')
    return
  }
  showToast(t('user_actions.followed', { name }), 'success')
}

function errorText(err: unknown): string {
  return String((err as Error)?.message ?? err)
}
