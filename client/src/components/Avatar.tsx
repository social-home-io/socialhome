/**
 * Session-presence indicator. Renders a coloured dot in the bottom-
 * right corner of the avatar when set:
 *   • ``'online'`` → green
 *   • ``'idle'``   → amber
 *   • ``null`` / undefined → no dot (offline = absent signal, not grey clutter)
 */
type OnlineStatus = 'online' | 'idle' | null

interface AvatarProps {
  /** Backend-signed URL — comes pre-tagged with ``?exp=&sig=`` so the
   *  browser can load it via raw ``<img src>`` without needing an
   *  ``Authorization`` header. */
  src?: string | null
  name: string
  size?: number
  onClick?: () => void
  online?: OnlineStatus
}

export function Avatar({ src, name, size = 40, onClick, online }: AvatarProps) {
  const initials = name.slice(0, 2).toUpperCase()
  const sizeStyle = { width: `${size}px`, height: `${size}px`, fontSize: `${size * 0.4}px` }
  const inner = src ? (
    <img
      src={src}
      alt={name}
      class={`sh-avatar ${onClick ? 'sh-avatar--clickable' : ''}`}
      width={size}
      height={size}
      onClick={onClick}
    />
  ) : (
    <div
      class={`sh-avatar sh-avatar--initials ${onClick ? 'sh-avatar--clickable' : ''}`}
      style={sizeStyle}
      onClick={onClick}
    >
      {initials}
    </div>
  )

  if (!online) return inner

  // Wrapper carries the dot. The inline label gives screen readers
  // context — sighted users get the colour cue from the dot itself.
  const label = online === 'idle' ? `${name} is idle` : `${name} is online`
  return (
    <span class="sh-avatar-wrap" aria-label={label} role="img">
      {inner}
      <span
        class={`sh-avatar-status-dot sh-avatar-status-dot--${online}`}
        aria-hidden="true"
      />
    </span>
  )
}
