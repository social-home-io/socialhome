/**
 * SpaceHero — the branded profile-style header at the top of a space
 * (Twitter / Facebook cover pattern).
 *
 * A wide cover banner (the admin's uploaded image, or a primary→accent
 * theme gradient when none is set) with the space's icon as a circular
 * avatar overlapping the banner's bottom-left, the name + member count
 * beside it, and the About markdown below. The avatar is the space's
 * uploaded icon image when set, otherwise its emoji (or name initial).
 *
 * ``slim`` renders a compact variant for non-feed tabs: a shorter banner +
 * smaller avatar + name, no About — branding presence without eating the
 * vertical space a tool tab (Calendar, Tasks…) needs.
 *
 * Styling lives in ``.sh-space-hero*`` (app.css); theme colours flow in via
 * the per-space CSS vars set by ``useSpaceTheme``.
 */
import { MarkdownView } from './MarkdownView'
import { isOne, t } from '@/i18n/i18n'

interface Props {
  name: string
  emoji: string | null
  coverUrl: string | null
  iconUrl?: string | null
  about: string | null
  memberCount?: number | null
  slim?: boolean
}

export function SpaceHero({
  name,
  emoji,
  coverUrl,
  iconUrl,
  about,
  memberCount,
  slim,
}: Props) {
  const fallback = emoji || name.trim().charAt(0).toUpperCase() || '🏠'
  return (
    <header class={slim ? 'sh-space-hero sh-space-hero--slim' : 'sh-space-hero'}>
      <div
        class={
          coverUrl
            ? 'sh-space-hero-banner'
            : 'sh-space-hero-banner sh-space-hero-banner--gradient'
        }
      >
        {coverUrl && <img class="sh-space-hero-image" src={coverUrl} alt="" />}
      </div>
      <div class="sh-space-hero-identity">
        <span class="sh-space-hero-avatar" aria-hidden="true">
          {iconUrl ? (
            <img class="sh-space-hero-avatar-img" src={iconUrl} alt="" />
          ) : (
            fallback
          )}
        </span>
        <div class="sh-space-hero-meta">
          <h2 class="sh-space-hero-name">{name}</h2>
          {!slim && memberCount != null && (
            <span class="sh-space-hero-members">
              {t(isOne(memberCount) ? 'space.header.members_one' : 'space.header.members',
                 { count: String(memberCount) })}
            </span>
          )}
        </div>
      </div>
      {!slim && about && (
        <div class="sh-space-hero-about">
          <MarkdownView src={about} />
        </div>
      )}
    </header>
  )
}
