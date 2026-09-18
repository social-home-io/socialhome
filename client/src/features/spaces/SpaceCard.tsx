/**
 * SpaceCard — shared card renderer for the three browser tabs
 * ("Your household" / "From friends" / "Global directory").
 *
 * Surfaces a scope chip (🏠 household / 🤝 public / 🌐 global),
 * a join-mode chip (🔓 Open / ✉ Approval required / 🎟 Invite-only),
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
 * / comment / react). Private household spaces do not show Subscribe.
 */
import { Button } from '@/components/Button'
import { categoryLabel, SPACE_CATEGORIES } from '@/components/spaceModeOptions'
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
      return { cls: 'sh-scope-chip sh-scope-chip--household', icon: '🏠', label: 'Household' }
    case 'public':
      return { cls: 'sh-scope-chip sh-scope-chip--public', icon: '🤝', label: 'Public' }
    case 'global':
      return { cls: 'sh-scope-chip sh-scope-chip--global', icon: '🌐', label: 'Global' }
  }
}

function joinModeChip(mode: DirectoryEntry['join_mode']) {
  switch (mode) {
    case 'open':
      return { cls: 'sh-join-mode-chip sh-join-mode-chip--open', icon: '🔓', label: 'Open to join' }
    case 'request':
      return { cls: 'sh-join-mode-chip sh-join-mode-chip--request', icon: '✉', label: 'Approval required' }
    case 'invite_only':
      // Not just a join gate: an invite-only space publishes nothing
      // publicly, so there is no content to read and nothing to subscribe
      // to. The old "Invite-only" read as "you may ask for an invite".
      return {
        cls: 'sh-join-mode-chip sh-join-mode-chip--invite',
        icon: '🎟',
        label: 'Invite-only · content is private',
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
 * Whether Subscribe is appropriate for this entry. Only LOCAL public /
 * global spaces (hosted by this household) support subscription, and only
 * when the user isn't already a real member. Subscribe is a local
 * self-service member-add with no remote federation path, so it's hidden
 * for remotely-hosted (friends / global) spaces — those use the join-request
 * flow instead.
 */
function subscribableScope(entry: DirectoryEntry): boolean {
  // Subscribe is a LOCAL self-service member-add: the backend
  // `/api/spaces/{id}/subscribe` requires a local space row, so it only
  // works for spaces this household hosts. Remote (friends/global) spaces
  // have no remote-subscribe federation path — showing the button there
  // just 404s; they're joined via the request flow instead.
  //
  // An invite-only space is never subscribable, local or not: it publishes
  // no content and hands out no content key, so a subscriber would sit on a
  // seat that never receives anything (the GFS refuses such a subscribe
  // outright with a 403).
  return (
    entry.host_instance_id === 'local'
    && (entry.scope === 'public' || entry.scope === 'global')
    && entry.join_mode !== 'invite_only'
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
    case 'open':        return 'Open space'
    case 'pending':     return 'Request pending'
    case 'invite-only': return 'Invite required'
    case 'join':        return 'Join'
    case 'request':     return 'Request to join'
    case 'pair-first':  return `Connect with ${hostLabel(entry)} first`
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
      aria-label={tappable ? `Open ${entry.name} details` : undefined}
    >
      <div class="sh-browser-card__hd">
        <span class="sh-space-emoji" aria-hidden="true">{entry.emoji || '🗂'}</span>
        <div class="sh-browser-card__title">
          <strong>{entry.name}</strong>
          <span class="sh-muted sh-browser-card__count">
            {entry.member_count} {entry.member_count === 1 ? 'member' : 'members'}
          </span>
        </div>
        {entry.already_subscribed && (
          <span
            class="sh-subscribed-pill"
            title="You receive this space's updates (read-only)"
            aria-label="Subscribed"
          >
            🔔 Subscribed
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
        {entry.min_age > 0 && (
          <span class="sh-age-chip" title={`Minimum age ${entry.min_age}`}>
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
          Hosted by <strong>{hostLabel(entry)}</strong>
          {!entry.host_is_paired && (
            <span class="sh-muted"> · not yet connected</span>
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
                ? `Subscribe to ${entry.name} (read-only updates)`
                : `Unsubscribe from ${entry.name}`
            }
            title={
              subscribeButton.kind === 'subscribe'
                ? 'Get this space\'s updates without joining. You won\'t be able to post.'
                : 'Stop receiving this space\'s updates.'
            }
            onClick={() => onAction(entry, subscribeButton)}
          >
            {subscribeBusy
              ? <span class="sh-spinner-sm" aria-hidden="true" />
              : subscribeButton.kind === 'subscribe'
                ? <><span aria-hidden="true">🔔</span> Subscribe</>
                : <><span aria-hidden="true">🔕</span> Unsubscribe</>}
          </button>
        )}
      </div>
    </article>
  )
}
