import { LogoMark } from './LogoMark'

/**
 * Wordmark — the canonical "Social Home" lockup, ported from the
 * marketing site's Header. "Social" inherits the surrounding ink
 * colour; "Home" is rendered in the hearth accent (`--sh-primary`)
 * via the `<em>` rule in app.css. An optional tagline renders below
 * in JetBrains Mono uppercase, matching the site masthead.
 *
 * Render `as="a"` to make the lockup a link back to "/" — the
 * sidebar uses this so the brand always returns the user home.
 */
interface WordmarkProps {
  size?: number
  tagline?: string
  as?: 'div' | 'a'
  href?: string
  className?: string
}

export function Wordmark({
  size = 28,
  tagline,
  as = 'div',
  href,
  className,
}: WordmarkProps) {
  const classes = ['sh-wordmark', className].filter(Boolean).join(' ')

  const content = (
    <>
      <LogoMark size={size} />
      <span class="sh-wordmark__set">
        <span class="sh-wordmark__name">
          Social <em>Home</em>
        </span>
        {tagline && <span class="sh-wordmark__motto">{tagline}</span>}
      </span>
    </>
  )

  if (as === 'a') {
    return (
      <a
        class={`${classes} sh-wordmark--link`}
        href={href ?? '/'}
        aria-label="Social Home — home"
      >
        {content}
      </a>
    )
  }
  return <div class={classes}>{content}</div>
}
