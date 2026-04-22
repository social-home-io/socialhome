/**
 * BotAvatar — circular avatar for bot-bridge posts.
 *
 * Two rendering modes, resolved by the shape of `bot.icon`:
 *   1. Emoji (most common) — render the emoji centred on a filled accent
 *      circle. Emoji can be arbitrary length (ZWJ sequences etc.) so we
 *      rely on font sizing rather than measuring.
 *   2. HA `entity_id` (string contains '.' and matches `domain.object`) —
 *      render an HA-style 🏠 fallback; the full icon resolution via
 *      platform adapter is a future enhancement that needs the admin UI
 *      to expose what icons are available.
 *
 * When `bot` is null the caller passed a bot-authored post whose bot has
 * been deleted; we render the Home Assistant monogram so the post still
 * has a visible provenance rather than an empty circle.
 */
import type { SpaceBotSummary } from '@/types'

interface BotAvatarProps {
  bot: SpaceBotSummary | null
  size?: number
}

function isLikelyEntityId(icon: string): boolean {
  // Loose match: "domain.object_name", lowercase. Anything else is an emoji.
  return /^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/.test(icon)
}

export function BotAvatar({ bot, size = 40 }: BotAvatarProps) {
  const px = `${size}px`
  if (bot === null) {
    return (
      <div
        class="sh-bot-avatar sh-bot-avatar--fallback"
        style={{ width: px, height: px, lineHeight: px, fontSize: `${size * 0.5}px` }}
        title="Home Assistant (bot deleted)"
        aria-label="Home Assistant"
      >
        🏠
      </div>
    )
  }
  if (isLikelyEntityId(bot.icon)) {
    return (
      <div
        class="sh-bot-avatar"
        style={{ width: px, height: px, lineHeight: px, fontSize: `${size * 0.5}px` }}
        title={`${bot.name} — ${bot.icon}`}
        aria-label={bot.name}
      >
        🏠
      </div>
    )
  }
  return (
    <div
      class={`sh-bot-avatar sh-bot-avatar--${bot.scope}`}
      style={{
        width: px,
        height: px,
        lineHeight: px,
        // Emojis render at ~60% of container so the accent ring stays visible.
        fontSize: `${size * 0.6}px`,
      }}
      title={bot.name}
      aria-label={bot.name}
    >
      {bot.icon}
    </div>
  )
}
