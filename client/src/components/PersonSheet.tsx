/**
 * PersonSheet — the one place to look at another person.
 *
 * Opened with :func:`openPerson` from any avatar, name or ⋯ menu; mounted
 * once in the app shell. It replaces the Friends chip sheet (Message /
 * Nickname) and the Highlights / Momentum ⋯ menu (Follow / Report /
 * Block), so every person action lives on one surface:
 *
 *   • Header — 72 px photo with the presence dot (local and remote, when
 *     known), the serif name, and "Their name: …" when the viewer set a
 *     private nickname.
 *   • Status pill + bio. An emoji-only status becomes a badge on the
 *     photo instead of a lonely pill.
 *   • Message (primary), then Call for your own household or Follow for
 *     everyone else.
 *   • Rows: Household, Follow their moments (own household), Edit
 *     nickname.
 *   • A quiet bottom line: Report… (other households) · Block… — or
 *     Unblock once blocked.
 *
 * Data: a caller that already holds the full row (the Friends page)
 * passes it as ``context.person`` and the sheet renders without a fetch.
 * Anyone else passes what it has (a name, a picture) and the sheet asks
 * ``GET /api/friends/{user_id}`` for the rest. A 404 there — someone the
 * viewer doesn't share a household connection with, blocked, or unknown
 * — leaves a minimal sheet built from the hint alone.
 *
 * Phones get the shared Modal's bottom-sheet variant (it sits above the
 * mobile nav and pads for the safe area); desktop a centred dialog. The
 * Modal owns the focus trap, Escape and return-focus.
 */
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { t, formatLocale } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { blockUser, unblockUser, isBlocked, blockedUsers, loadBlocks } from '@/store/blocks'
import { followUser, unfollowUser, isFollowing, loadFollows } from '@/store/follows'
import { ConnIcon } from '@/features/connections/ConnIcon'
import type { UserStatus } from '@/types'
import { Modal } from './Modal'
import { Avatar } from './Avatar'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { showToast } from './Toast'
import { openReport } from './ReportDialog'
import { openAliasDialog } from './AliasDialog'
import { openCallTypePicker } from './CallTypePickerDialog'
import { openStatusEditor } from './StatusEditor'

/** Where a person lives, as ``GET /api/friends/{user_id}`` returns it. */
export interface PersonHousehold {
  instance_id: string | null
  display_name: string
  /** The viewer's own household. */
  is_own: boolean
}

/** Everything the sheet can show about one person. Only ``user_id`` is
 *  guaranteed; the rest fills in from the caller's hint and the fetch. */
export interface PersonView {
  user_id: string
  display_name?: string | null
  /** The viewer's private nickname for them (§4.1.6). */
  personal_alias?: string | null
  picture_url?: string | null
  /** Lives in the viewer's household. Decides how a DM is addressed
   *  (``{username}`` vs ``{user_id}``) and whether Call is offered. */
  is_local?: boolean
  /** Local members only — the DM endpoint addresses them by username. */
  username?: string | null
  is_online?: boolean
  is_idle?: boolean
  status?: UserStatus | null
  bio?: string | null
  household?: PersonHousehold | null
}

export interface PersonContext {
  /** What the caller already knows. A hint with ``is_local``, a
   *  ``household`` and a name is treated as complete — no fetch. */
  person?: Omit<Partial<PersonView>, 'user_id'>
  /** Fired after the viewer saves or clears a nickname, so the opener
   *  can patch its own row without a reload. */
  onAliasChanged?: (userId: string, alias: string | null) => void
}

type LoadState = 'ready' | 'loading' | 'unavailable'

interface SheetState {
  userId: string
  person: PersonView
  load: LoadState
  onAliasChanged?: PersonContext['onAliasChanged']
}

const sheet = signal<SheetState | null>(null)
const busy = signal<'message' | 'call' | 'follow' | null>(null)
const confirmBlock = signal(false)
/** Bumps on every open so a slow fetch for an earlier person never
 *  paints over the one on screen now. */
let openSeq = 0

function isComplete(p: Partial<PersonView>): boolean {
  return p.is_local !== undefined && !!p.household && !!p.display_name
}

/**
 * Open the person sheet for ``userId``.
 *
 * ``context.person`` pre-fills the sheet (a name and picture are enough
 * for an instant first paint); anything missing is fetched. A caller
 * that only has a raw id can omit ``context`` entirely.
 */
export function openPerson(userId: string, context: PersonContext = {}): void {
  const seq = ++openSeq
  const hint = { ...(context.person ?? {}) }
  // Callers resolve names with helpers that fall back to the raw id —
  // a name that *is* the id is no name at all.
  if (hint.display_name === userId) hint.display_name = null
  const person: PersonView = { ...hint, user_id: userId }
  const complete = isComplete(person)
  busy.value = null
  confirmBlock.value = false
  sheet.value = {
    userId,
    person,
    load: complete ? 'ready' : 'loading',
    onAliasChanged: context.onAliasChanged,
  }
  void loadBlocks()
  void loadFollows()
  if (complete) return
  api.get(`/api/friends/${encodeURIComponent(userId)}`).then((row) => {
    if (seq !== openSeq || !sheet.value) return
    sheet.value = {
      ...sheet.value,
      person: { ...sheet.value.person, ...(row as PersonView), user_id: userId },
      load: 'ready',
    }
  }).catch(() => {
    if (seq !== openSeq || !sheet.value) return
    sheet.value = { ...sheet.value, load: 'unavailable' }
  })
}

/** Close the sheet (no-op when it isn't open). */
export function closePerson(): void {
  openSeq++
  sheet.value = null
  confirmBlock.value = false
  busy.value = null
}

/** "until 17:00" today, "until Sun 17:00" later. */
function untilLabel(iso: string | null | undefined): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const time = d.toLocaleTimeString(formatLocale(), {
    hour: '2-digit', minute: '2-digit',
  })
  const when = sameDay
    ? time
    : `${d.toLocaleDateString(formatLocale(), { weekday: 'short' })} ${time}`
  return t('person.status_until', { time: when })
}

function errText(e: unknown): string {
  return String((e as Error)?.message ?? e)
}

export function PersonSheet() {
  const loc = useLocation()
  const s = sheet.value
  if (!s) return null
  const p = s.person
  const me = currentUser.value?.user_id ?? null
  const isSelf = me !== null && p.user_id === me
  const blocked = isBlocked(p.user_id)
  const blockEntry = blocked
    ? blockedUsers.value.find(b => b.user_id === p.user_id)
    : undefined
  const realName = p.display_name || blockEntry?.display_name || null
  const alias = p.personal_alias ?? blockEntry?.personal_alias ?? null
  const shown = alias || realName || t('person.someone')
  const aliased = !!alias && !!realName && alias !== realName
  const picture = p.picture_url ?? blockEntry?.picture_url ?? null
  const household = p.household
    ?? (blockEntry?.household
      ? { instance_id: null, display_name: blockEntry.household, is_own: false }
      : null)
  const isOwnHousehold = p.is_local === true || household?.is_own === true
  const limited = s.load === 'unavailable' || blocked
  // Until the fetch settles we don't know local vs remote, so actions
  // wait rather than flicker from Follow to Call.
  const settled = s.load !== 'loading'
  const following = isFollowing(p.user_id)

  const status = !limited ? p.status : null
  const statusText = status?.text?.trim() || null
  const statusEmoji = status?.emoji?.trim() || null
  const emojiOnly = !!statusEmoji && !statusText
  const until = statusText ? untilLabel(status?.expires_at) : null
  const presence = !limited && p.is_online ? (p.is_idle ? 'idle' : 'online') : null

  // Message needs to know how to address them: local by username,
  // remote by user id. Unknown → no Message rather than a failing one.
  // Nor when the lookup 404'd (someone only met through a shared space):
  // the household can't vouch a DM would reach them, whatever the hint.
  const canMessage = !isSelf && !blocked && s.load !== 'unavailable'
    && (p.is_local === true ? !!p.username : p.is_local === false)
  const canCall = canMessage && p.is_local === true

  const openDm = async (): Promise<string> => {
    const body = p.is_local ? { username: p.username } : { user_id: p.user_id }
    const conv = await api.post('/api/conversations/dm', body) as { id: string }
    return conv.id
  }

  const onMessage = async () => {
    if (busy.value) return
    busy.value = 'message'
    try {
      const id = await openDm()
      closePerson()
      loc.route(`/dms/${id}`)
    } catch (e: unknown) {
      showToast((e as Error)?.message || t('friends.dm_failed'), 'error')
    } finally {
      busy.value = null
    }
  }

  const onCall = async () => {
    if (busy.value) return
    busy.value = 'call'
    try {
      const id = await openDm()
      closePerson()
      openCallTypePicker(id)
    } catch (e: unknown) {
      showToast((e as Error)?.message || t('calls.start_failed'), 'error')
    } finally {
      busy.value = null
    }
  }

  const onToggleFollow = async () => {
    if (busy.value) return
    busy.value = 'follow'
    try {
      if (following) {
        await unfollowUser(p.user_id)
        showToast(t('user_actions.unfollowed', { name: shown }), 'success')
      } else {
        await followUser(p.user_id)
        showToast(t('user_actions.followed', { name: shown }), 'success')
      }
    } catch (e: unknown) {
      showToast(t(
        following ? 'user_actions.unfollow_failed' : 'user_actions.follow_failed',
        { error: errText(e) },
      ), 'error')
    } finally {
      busy.value = null
    }
  }

  const onRename = () => {
    const onAliasChanged = s.onAliasChanged
    const userId = p.user_id
    const args = {
      targetUserId: userId,
      globalDisplayName: realName ?? shown,
      currentAlias: alias,
      onSave: (next: string | null) => { onAliasChanged?.(userId, next) },
    }
    // Close first so the nickname dialog opens over an empty stack and
    // focus returns to whatever opened the sheet.
    closePerson()
    openAliasDialog(args)
  }

  const onReport = () => {
    const userId = p.user_id
    closePerson()
    openReport('user', userId)
  }

  const onBlock = async () => {
    try {
      await blockUser(p.user_id)
      showToast(t('user_actions.blocked', { name: shown }), 'success')
      closePerson()
    } catch (e: unknown) {
      confirmBlock.value = false
      showToast(t('user_actions.block_failed', { error: errText(e) }), 'error')
    }
  }

  const onUnblock = async () => {
    try {
      await unblockUser(p.user_id)
      showToast(t('user_actions.unblocked', { name: shown }), 'success')
      closePerson()
    } catch (e: unknown) {
      showToast(t('user_actions.unblock_failed', { error: errText(e) }), 'error')
    }
  }

  const header = (titleId: string) => (
    <div class="sh-person-sheet__id">
      <span class="sh-person-sheet__photo">
        <Avatar name={shown} src={picture} size={72} online={presence} />
        {emojiOnly && (
          <span
            class="sh-person-sheet__badge"
            role="img"
            aria-label={t('person.status_badge', { status: statusEmoji ?? '' })}
            data-testid="person-status-badge"
          >
            {statusEmoji}
          </span>
        )}
      </span>
      <div class="sh-person-sheet__names">
        <h2 id={titleId} class="sh-person-sheet__name">
          {shown}
          {isSelf && <span class="sh-person-sheet__you"> ({t('friends.you')})</span>}
        </h2>
        {aliased && (
          <p class="sh-person-sheet__sub" data-testid="person-real-name">
            {t('friends.their_name', { name: realName ?? '' })}
            {' · '}
            {t('person.nickname_only_you')}
          </p>
        )}
      </div>
      <button
        type="button"
        class="sh-modal-close sh-person-sheet__close"
        onClick={closePerson}
        aria-label={t('modal.close')}
      >
        &times;
      </button>
    </div>
  )

  return (
    <>
      <Modal
        open={true}
        onClose={closePerson}
        title={shown}
        class="sh-person-sheet"
        header={header}
      >
        <div
          class="sh-person-sheet__body"
          aria-busy={s.load === 'loading' ? 'true' : undefined}
          data-testid="person-sheet"
          data-user-id={p.user_id}
        >
          {(statusText || p.bio) && !limited && (
            <div class="sh-person-sheet__about">
              {statusText && (
                <p class="sh-person-sheet__status" data-testid="person-status">
                  {statusEmoji && <span aria-hidden="true">{statusEmoji}</span>}
                  <span class="sh-person-sheet__status-text">{statusText}</span>
                  {until && <span class="sh-person-sheet__until">· {until}</span>}
                </p>
              )}
              {p.bio && <p class="sh-person-sheet__bio">{p.bio}</p>}
            </div>
          )}

          {limited && (
            <p class="sh-person-sheet__note sh-muted" data-testid="person-limited">
              {blocked
                ? t('person.blocked_note', { name: shown })
                : t('person.limited_note')}
            </p>
          )}

          {s.load === 'loading' && (
            <p class="sh-person-sheet__note sh-muted" role="status">
              {t('common.loading')}
            </p>
          )}

          {settled && (isSelf ? (
            <div class="sh-person-sheet__actions">
              <Button
                onClick={() => { closePerson(); loc.route('/settings') }}
                data-testid="person-edit-profile"
              >
                {t('person.edit_profile')}
              </Button>
              <Button
                variant="secondary"
                onClick={() => { closePerson(); openStatusEditor() }}
                data-testid="person-edit-status"
              >
                {t('person.edit_status')}
              </Button>
            </div>
          ) : (canMessage || (!blocked && !isOwnHousehold)) && (
            <div class="sh-person-sheet__actions">
              {canMessage && (
                <Button
                  onClick={onMessage}
                  loading={busy.value === 'message'}
                  data-testid="person-message"
                >
                  <ConnIcon name="message" />
                  {t('friends.message')}
                </Button>
              )}
              {canCall && (
                <Button
                  variant="secondary"
                  onClick={onCall}
                  loading={busy.value === 'call'}
                  data-testid="person-call"
                >
                  <ConnIcon name="phone" />
                  {t('person.call')}
                </Button>
              )}
              {!blocked && !isOwnHousehold && (
                <Button
                  variant="secondary"
                  onClick={onToggleFollow}
                  loading={busy.value === 'follow'}
                  aria-pressed={following ? 'true' : 'false'}
                  data-testid="person-follow"
                >
                  {following && <ConnIcon name="check" />}
                  {following ? t('person.following') : t('user_actions.follow')}
                </Button>
              )}
            </div>
          ))}

          {settled && (
            <div class="sh-person-sheet__rows">
              {household && (
                <div class="sh-person-sheet__row" data-testid="person-household">
                  <ConnIcon name="home" />
                  <span class="sh-person-sheet__row-body">
                    <span class="sh-person-sheet__row-label">{t('person.household')}</span>
                    <span class="sh-person-sheet__row-value">
                      {household.display_name}
                      {household.is_own && ` · ${t('friends.your_household')}`}
                    </span>
                  </span>
                </div>
              )}
              {!isSelf && !blocked && isOwnHousehold && (
                <button
                  type="button"
                  class="sh-person-sheet__row sh-person-sheet__row--btn"
                  onClick={onToggleFollow}
                  disabled={busy.value === 'follow'}
                  aria-pressed={following ? 'true' : 'false'}
                  data-testid="person-follow"
                >
                  <ConnIcon name={following ? 'check' : 'plus'} />
                  <span class="sh-person-sheet__row-body">
                    <span class="sh-person-sheet__row-value">
                      {following ? t('person.following_moments') : t('person.follow_moments')}
                    </span>
                  </span>
                  <span />
                </button>
              )}
              {!isSelf && !blocked && (
                <button
                  type="button"
                  class="sh-person-sheet__row sh-person-sheet__row--btn"
                  onClick={onRename}
                  data-testid="person-nickname"
                >
                  <ConnIcon name="tag" />
                  <span class="sh-person-sheet__row-body">
                    <span class="sh-person-sheet__row-value">
                      {alias ? t('friends.edit_nickname') : t('friends.set_nickname')}
                    </span>
                  </span>
                  <span class="sh-person-sheet__chev"><ConnIcon name="chev" /></span>
                </button>
              )}
            </div>
          )}

          {settled && !isSelf && (
            <div class="sh-person-sheet__quiet">
              {blocked ? (
                <button type="button" onClick={onUnblock} data-testid="person-unblock">
                  {t('user_actions.unblock')}
                </button>
              ) : (
                <>
                  {!isOwnHousehold && (
                    <button type="button" onClick={onReport} data-testid="person-report">
                      {t('person.report')}
                    </button>
                  )}
                  <button
                    type="button"
                    class="sh-person-sheet__danger"
                    onClick={() => { confirmBlock.value = true }}
                    data-testid="person-block"
                  >
                    {t('person.block')}
                  </button>
                </>
              )}
            </div>
          )}
        </div>
      </Modal>
      <ConfirmDialog
        open={confirmBlock.value}
        title={t('user_actions.block_title', { name: shown })}
        message={t('user_actions.block_body')}
        confirmLabel={t('user_actions.block_confirm')}
        destructive
        onConfirm={onBlock}
        onCancel={() => { confirmBlock.value = false }}
      />
    </>
  )
}
