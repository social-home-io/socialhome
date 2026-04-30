interface AvatarProps {
  /** Backend-signed URL — comes pre-tagged with ``?exp=&sig=`` so the
   *  browser can load it via raw ``<img src>`` without needing an
   *  ``Authorization`` header. */
  src?: string | null
  name: string
  size?: number
  onClick?: () => void
}

export function Avatar({ src, name, size = 40, onClick }: AvatarProps) {
  const initials = name.slice(0, 2).toUpperCase()
  const sizeStyle = { width: `${size}px`, height: `${size}px`, fontSize: `${size * 0.4}px` }
  if (src) {
    return (
      <img
        src={src}
        alt={name}
        class={`sh-avatar ${onClick ? 'sh-avatar--clickable' : ''}`}
        width={size}
        height={size}
        onClick={onClick}
      />
    )
  }
  return (
    <div
      class={`sh-avatar sh-avatar--initials ${onClick ? 'sh-avatar--clickable' : ''}`}
      style={sizeStyle}
      onClick={onClick}
    >
      {initials}
    </div>
  )
}
