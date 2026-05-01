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
    case 'link':
    case 'invite_only':
      return { cls: 'sh-join-mode-chip sh-join-mode-chip--invite', icon: '🎟', label: 'Invite-only' }
  }
}

function decidePrimary(entry: DirectoryEntry): SpaceCardAction {
  if (entry.already_member) return { kind: 'open' }
  if (entry.request_pending) return { kind: 'pending' }
  if (entry.join_mode === 'invite_only' || entry.join_mode === 'link') return { kind: 'invite-only' }
  // Remote + unpaired = must pair first before joining / requesting.
  if (entry.scope === 'global' && !entry.host_is_paired) return { kind: 'pair-first' }
  if (entry.scope === 'public' && !entry.host_is_paired) return { kind: 'pair-first' }
  return entry.join_mode === 'open' ? { kind: 'join' } : { kind: 'request' }
}

/**
 * Whether Subscribe is appropriate for this entry. Only public /
 * global spaces support subscription, and only when the user isn't
 * already a real member. Paired host or "local" are both fine —
 * subscribe is a self-service member-add that doesn't need admin
 * approval.
 */
function subscribableScope(entry: DirectoryEntry): boolean {
  return (
    (entry.scope === 'public' || entry.scope === 'global')
    && !entry.already_member
  )
}

function primaryLabel(action: SpaceCardAction, entry: DirectoryEntry): string {
  switch (action.kind) {
    case 'open':        return 'Open space'
    case 'pending':     return 'Request pending'
    case 'invite-only': return 'Invite required'
    case 'join':        return 'Join'
    case 'request':     return 'Request to join'
    case 'pair-first':  return `Connect with ${entry.host_display_name} first`
    // Subscribe/unsubscribe never appear as the primary action.
    case 'subscribe':
    case 'unsubscribe': return ''
  }
}

export function SpaceCard({ entry, onAction, subscribeBusy = false }: SpaceCardProps) {
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

  return (
    <article class={`sh-browser-card sh-browser-card--${entry.scope}`}>
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
      </div>
      {entry.scope !== 'household' && (
        <p class="sh-host-callout sh-muted">
          Hosted by <strong>{entry.host_display_name}</strong>
          {!entry.host_is_paired && (
            <span class="sh-muted"> · not yet connected</span>
          )}
        </p>
      )}
      <div class="sh-browser-card__actions">
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
