/**
 * SpaceCard — shared card renderer for the three browser tabs
 * ("Your household" / "From friends" / "Global directory").
 *
 * Surfaces a scope chip (🏠 household / 🤝 public / 🌐 global),
 * a join-mode chip (🔓 Open / ✉ Approval required / 🎟 Invite-only),
 * a 🔒 "Content is private" chip when the space takes no followers,
 * an optional age chip (13+ / 16+ / 18+), the "Hosted by …" line for
 * remote spaces, and routes the action button between states:
 *
 *   • already-member          → "Open space"
 *   • already-subscribed      → "Open space" + "Unsubscribe" secondary
 *   • request pending         → disabled "Request pending"
 *   • open + local-or-paired  → "Join"
 *   • request + local-or-paired → "Request to join" (opens modal)
 *   • invite-only             → disabled "Invite required"
 *   • remote + unpaired host  → "Connect with {household} first"
 *
 * Public + global spaces also surface a secondary "🔔 Subscribe" button
 * in the non-member states — subscription = read-only member (no post
 * / comment / react). Private household spaces do not show Subscribe,
 * and neither do spaces whose owner has not opted into followers (see
 * {@link contentIsGated}).
 */
import { Button } from '@/components/Button'
import { categoryLabel, SPACE_CATEGORIES } from '@/components/spaceModeOptions'
import { isOne, t } from '@/i18n/i18n'
import type { DirectoryEntry } from '@/types'

export type SpaceCardAction =
  | { kind: 'join' }
  | { kind: 'request' }
  | { kind: 'open' }
  | { kind: 'pending' }
  | { kind: 'invite-only' }
  | { kind: 'pair-first' }
  | { kind: 'subscribe' }
  | { kind: 'unsubscribe' }

export interface SpaceCardProps {
  entry: DirectoryEntry
  /** Invoked when the user clicks a card action; caller decides what
   *  to do with the {@link DirectoryEntry} + action kind. */
  onAction: (entry: DirectoryEntry, action: SpaceCardAction) => void
  /** When the subscribe / unsubscribe action is in-flight the button
   *  is disabled + shows a spinner. Keyed by space_id so the browser
   *  page can track multiple in-flight toggles. */
  subscribeBusy?: boolean
  /** Tap the card body (anywhere outside the action buttons) to drill
   *  into the public detail page.  The browser page wires this to
   *  ``loc.route('/spaces/:id/about')``; passing ``undefined`` keeps
   *  the legacy behaviour where only the action button is interactive. */
  onCardTap?: (entry: DirectoryEntry) => void
}

function scopeChip(scope: DirectoryEntry['scope']) {
  switch (scope) {
    case 'household':
      return { cls: 'sh-scope-chip sh-scope-chip--household', icon: '🏠', label: t('space.visibility.household') }
    case 'public':
      return { cls: 'sh-scope-chip sh-scope-chip--public', icon: '🤝', label: t('space.visibility.public') }
    case 'global':
      return { cls: 'sh-scope-chip sh-scope-chip--global', icon: '🌐', label: t('space.visibility.global') }
  }
}

/**
 * Whether this space is KNOWN to withhold its content from non-members.
 *
 * Keyed on `allow_subscribers` — the owner's explicit readability opt-in
 * (`SpaceFeatures.allow_subscribers` in `socialhome/domain/space.py`), NOT
 * on the join mode. The two are independent dials: an invite-only space with
 * followers enabled is a broadcast space (invited people post, anyone may
 * read), and an open-to-join space with followers disabled is joinable but
 * not publicly readable. With the flag off nothing is relayed and no content
 * key is sealed to a subscriber, so there is nothing to subscribe to.
 *
 * Strict `=== false`, deliberately: the "Your household" and "Global
 * directory" tabs always map a concrete boolean (`/api/spaces` ships the
 * space's features, `/api/public_spaces` the GFS's flag — each failing
 * closed to `false` at the mapper), while the peer "From friends" directory
 * does not carry the flag yet. Claiming "content is private" for an
 * open friend-hosted space would be a lie; saying nothing is honest, and
 * costs nothing — a peer-hosted space is never subscribable from here
 * anyway (see {@link subscribableScope}, which requires an explicit `true`).
 *
 * Public/global scope only. A `household` space is private by definition —
 * never published, never relayed, never subscribable — so the flag carries no
 * information there, and the "Your household" tab maps it off every local
 * space's features. Without this check every private space on that tab wore a
 * 🔒 "Content is private" chip, announcing the default on every card and
 * teaching the reader to ignore the one place it matters.
 */
export function contentIsGated(entry: DirectoryEntry): boolean {
  return entry.scope !== 'household' && entry.allow_subscribers === false
}

/** The 🔒 chip that says so, rendered only when {@link contentIsGated}.
 *  ``label`` is a getter so it follows the current UI language. */
export const GATED_CHIP = {
  cls:   'sh-join-mode-chip sh-join-mode-chip--private',
  icon:  '🔒',
  get label(): string { return t('space.card.content_private') },
}

/**
 * The join-mode chip is pure MEMBERSHIP information: how a person becomes
 * someone who can post here. Readability is a separate chip (see
 * {@link GATED_CHIP}), because the two no longer imply each other.
 */
export function joinModeChip(mode: DirectoryEntry['join_mode']) {
  switch (mode) {
    case 'open':
      return { cls: 'sh-join-mode-chip sh-join-mode-chip--open', icon: '🔓', label: t('space.card.open_to_join') }
    case 'request':
      return {
        cls: 'sh-join-mode-chip sh-join-mode-chip--request',
        icon: '✉',
        label: t('space.card.approval_required'),
      }
    case 'invite_only':
      return {
        cls: 'sh-join-mode-chip sh-join-mode-chip--invite',
        icon: '🎟',
        label: t('space.card.invite_only'),
      }
  }
}

function decidePrimary(entry: DirectoryEntry): SpaceCardAction {
  if (entry.already_member) return { kind: 'open' }
  if (entry.request_pending) return { kind: 'pending' }
  if (entry.join_mode === 'invite_only') return { kind: 'invite-only' }
  // Remote + unpaired = must pair first before joining / requesting.
  if (entry.scope === 'global' && !entry.host_is_paired) return { kind: 'pair-first' }
  if (entry.scope === 'public' && !entry.host_is_paired) return { kind: 'pair-first' }
  return entry.join_mode === 'open' ? { kind: 'join' } : { kind: 'request' }
}

/**
 * Whether Subscribe is appropriate for this entry.
 *
 * Two kinds of space can be followed, and both go through the same
 * `POST /api/spaces/{id}/subscribe`:
 *
 *   • a public / global space this household HOSTS (`host_instance_id ===
 *     'local'`) — a local self-service member-add; and
 *   • a **global** space discovered through a connection server — the host
 *     mirrors as a local stub and the GFS seats the subscriber, which is the
 *     entire point of the global directory. Pairing with the host is NOT a
 *     prerequisite: the content arrives over the GFS relay, which is why the
 *     card can offer Subscribe next to a "Connect with … first" primary.
 *
 * Peer "From friends" spaces are excluded: they are not relayed through a
 * GFS, so there is no remote-subscribe path and the button would 404. Those
 * are joined through the request flow instead.
 *
 * In every case the owner must have opted into followers and the viewer must
 * not already be a real member. A space whose owner has not opted in is never
 * subscribable, local or not: it publishes no content and hands out no
 * content key, so a subscriber would sit on a seat that never receives
 * anything — both `/api/spaces/{id}/subscribe` and the GFS refuse it (403,
 * "this space does not allow subscribers").
 */
function subscribableScope(entry: DirectoryEntry): boolean {
  const reachable =
    entry.scope === 'global'
    || (entry.host_instance_id === 'local' && entry.scope === 'public')
  return (
    reachable
    // Explicit `true` — an absent flag is never enough to offer Subscribe.
    && entry.allow_subscribers === true
    && !entry.already_member
  )
}

/**
 * Human-readable label for the host household.
 *
 * A space discovered through a Global Federation Server is usually hosted
 * by a household we've never paired with, so the backend has no name for
 * it and falls back to the raw instance id (``host_display_name = iid``,
 * see ``routes/public_spaces.py``). Those ids are 52-char base32 strings
 * with no break opportunities, so rendering one inline blows straight
 * through the card. Shorten it to the same ``abcd1234…`` form the rest of
 * the app uses for unknown instances (``RemoteInviteInboxBanner``,
 * ``AutoPairDialog``); a real display name is shown in full.
 */
export function hostLabel(entry: DirectoryEntry): string {
  const name = entry.host_display_name
  if (!name || name === entry.host_instance_id) {
    return `${entry.host_instance_id.slice(0, 8)}…`
  }
  return name
}

function primaryLabel(action: SpaceCardAction, entry: DirectoryEntry): string {
  switch (action.kind) {
    case 'open':        return t('space.card.open_space')
    case 'pending':     return t('space.card.request_pending')
    case 'invite-only': return t('space.card.invite_required')
    case 'join':        return t('space.card.join')
    case 'request':     return t('space.join.request')
    case 'pair-first':  return t('space.card.connect_first', { name: hostLabel(entry) })
    // Subscribe/unsubscribe never appear as the primary action.
    case 'subscribe':
    case 'unsubscribe': return ''
  }
}

export function SpaceCard({
  entry, onAction, subscribeBusy = false, onCardTap,
}: SpaceCardProps) {
  const scope = scopeChip(entry.scope)
  const jmode = joinModeChip(entry.join_mode)
  const primary = decidePrimary(entry)
  const primaryDisabled = primary.kind === 'pending' || primary.kind === 'invite-only'
  const canSubscribe = subscribableScope(entry)
  const subscribeButton: SpaceCardAction | null =
    entry.already_subscribed
      ? { kind: 'unsubscribe' }
      : canSubscribe
        ? { kind: 'subscribe' }
        : null
  const tappable = !!onCardTap
  const tap = (ev: MouseEvent | KeyboardEvent) => {
    if (!onCardTap) return
    ev.preventDefault()
    onCardTap(entry)
  }

  return (
    <article
      class={
        `sh-browser-card sh-browser-card--${entry.scope}`
        + (tappable ? ' sh-browser-card--tappable' : '')
      }
      role={tappable ? 'link' : undefined}
      tabIndex={tappable ? 0 : undefined}
      onClick={tappable ? (e: MouseEvent) => tap(e) : undefined}
      onKeyDown={tappable ? (e: KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') tap(e)
      } : undefined}
      aria-label={tappable ? t('space.card.open_details_aria', { name: entry.name }) : undefined}
    >
      <div class="sh-browser-card__hd">
        <span class="sh-space-emoji" aria-hidden="true">{entry.emoji || '🗂'}</span>
        <div class="sh-browser-card__title">
          <strong>{entry.name}</strong>
          <span class="sh-muted sh-browser-card__count">
            {t(isOne(entry.member_count) ? 'space.members.count_one' : 'space.members.count', { n: String(entry.member_count) })}
          </span>
        </div>
        {entry.already_subscribed && (
          <span
            class="sh-subscribed-pill"
            title={t('spaces.list.subscribed_pill_title')}
            aria-label={t('spaces.list.subscribed_pill')}
          >
            🔔 {t('spaces.list.subscribed_pill')}
          </span>
        )}
      </div>
      {entry.description && (
        <p class="sh-browser-card__desc sh-muted">{entry.description}</p>
      )}
      <div class="sh-browser-card__chips">
        <span class={scope.cls} title={scope.label}>
          <span aria-hidden="true">{scope.icon}</span> {scope.label}
        </span>
        <span class={jmode.cls} title={jmode.label}>
          <span aria-hidden="true">{jmode.icon}</span> {jmode.label}
        </span>
        {contentIsGated(entry) && (
          <span
            class={GATED_CHIP.cls}
            title={t('space.card.content_private_title')}
          >
            <span aria-hidden="true">{GATED_CHIP.icon}</span> {GATED_CHIP.label}
          </span>
        )}
        {entry.min_age > 0 && (
          <span class="sh-age-chip" title={t('space.card.min_age_title', { n: String(entry.min_age) })}>
            {entry.min_age}+
          </span>
        )}
        {entry.category
          && SPACE_CATEGORIES.some(c => c.value === entry.category && c.value !== 'general') && (
          <span class="sh-age-chip" title={categoryLabel(entry.category)}>
            {categoryLabel(entry.category)}
          </span>
        )}
      </div>
      {entry.scope !== 'household' && (
        <p class="sh-host-callout sh-muted">
          {t('space.card.hosted_by')} <strong>{hostLabel(entry)}</strong>
          {!entry.host_is_paired && (
            <span class="sh-muted"> · {t('space.card.not_connected')}</span>
          )}
        </p>
      )}
      <div
        class="sh-browser-card__actions"
        onClick={(ev) => ev.stopPropagation()}
      >
        <Button
          variant={primary.kind === 'open' ? 'primary' : 'secondary'}
          disabled={primaryDisabled}
          onClick={() => onAction(entry, primary)}
        >
          {primaryLabel(primary, entry)}
        </Button>
        {subscribeButton && (
          <button
            type="button"
            class={
              'sh-subscribe-btn'
              + (subscribeButton.kind === 'unsubscribe'
                ? ' sh-subscribe-btn--on'
                : '')
            }
            disabled={subscribeBusy}
            aria-pressed={subscribeButton.kind === 'unsubscribe'}
            aria-label={
              subscribeButton.kind === 'subscribe'
                ? t('space.card.follow_aria', { name: entry.name })
                : t('spaces.list.unsubscribe_aria', { name: entry.name })
            }
            title={
              subscribeButton.kind === 'subscribe'
                ? t('space.card.follow_title')
                : t('space.subscriber.unsubscribe_title')
            }
            onClick={() => onAction(entry, subscribeButton)}
          >
            {subscribeBusy
              ? <span class="sh-spinner-sm" aria-hidden="true" />
              : subscribeButton.kind === 'subscribe'
                ? <><span aria-hidden="true">🔔</span> {t('space.card.follow')}</>
                : <><span aria-hidden="true">🔕</span> {t('space.subscriber.unsubscribe')}</>}
          </button>
        )}
      </div>
    </article>
  )
}
