/**
 * SpaceHero — the card at the top of a space's Feed section.
 *
 * Top to bottom:
 * - the **cover** — only when the admin set one (no grey slab without);
 * - the **identity row** — the space's icon (uploaded image, else its
 *   emoji, else the name's initial) and its name as a heading that wraps
 *   and is never truncated (``overflow-wrap: break-word``, no automatic
 *   hyphenation: proper nouns must not split);
 * - **one line** — a facepile of up to four members with the member
 *   count, one 36 px link to the Members section, then the space's tier
 *   ("· Private");
 * - the **About**, clamped to two lines, with "Read more" when it is
 *   longer — the full text opens in a dialog (MarkdownView, so external
 *   images stay blocked there too);
 * - the **quick links** as 36 px chips in one row that scrolls sideways
 *   (a plain link icon — never a fetched favicon).
 *
 * The other sections show no hero: the top bar and the sidebar already
 * name the space.
 *
 * Styling: ``.sh-space-hero*`` (app.css), logical properties only, so it
 * mirrors under ``dir=rtl``.
 */
import { useLayoutEffect, useRef, useState } from 'preact/hooks'
import { MarkdownView } from './MarkdownView'
import { Modal } from './Modal'
import { Avatar } from './Avatar'
import { isOne, t } from '@/i18n/i18n'
import type { QuickLink } from '@/features/spaces/useSpaceLinks'

/** A face in the facepile. */
export interface HeroMember {
  user_id: string
  name: string
  picture_url: string | null
}

/** The four tiers a space can have (``SpaceType``). */
export type SpaceTier = 'private' | 'household' | 'public' | 'global'

interface Props {
  name: string
  emoji: string | null
  coverUrl: string | null
  iconUrl?: string | null
  about: string | null
  memberCount?: number | null
  /** Up to four members for the facepile, in roster order. */
  members?: readonly HeroMember[]
  /** Where the facepile leads (the space's Members section). */
  membersHref?: string
  tier?: string | null
  links?: readonly QuickLink[]
}

/** "Private" / "Household" / "Public" / "Global" — ``null`` for a tier
 *  this build doesn't know. Literal keys so the i18n check sees them. */
export function tierLabel(tier: string | null | undefined): string | null {
  switch (tier) {
    case 'private':   return t('space.visibility.private')
    case 'household': return t('space.visibility.household')
    case 'public':    return t('space.visibility.public')
    case 'global':    return t('space.visibility.global')
    default:          return null
  }
}

const LINK_ICON = (
  <svg class="sh-space-hero-link__icon" viewBox="0 0 24 24" width="14" height="14" fill="none"
       stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
       aria-hidden="true" focusable="false">
    <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" />
    <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />
  </svg>
)

export function SpaceHero({
  name, emoji, coverUrl, iconUrl, about, memberCount, members = [], membersHref,
  tier, links = [],
}: Props) {
  const fallback = emoji || name.trim().charAt(0).toUpperCase() || '🏠'
  const aboutRef = useRef<HTMLDivElement>(null)
  // "Read more" only when the two-line clamp actually hides something.
  const [clamped, setClamped] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)

  useLayoutEffect(() => {
    const el = aboutRef.current
    if (!el) { setClamped(false); return }
    const measure = () => setClamped(el.scrollHeight > el.clientHeight + 1)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [about])

  const scope = tierLabel(tier)
  const countLabel = memberCount != null
    ? t(isOne(memberCount) ? 'space.header.members_one' : 'space.header.members',
        { count: String(memberCount) })
    : null
  const faces = members.slice(0, 4)

  return (
    <header class={coverUrl ? 'sh-space-hero sh-space-hero--cover' : 'sh-space-hero'}>
      {coverUrl && (
        <div class="sh-space-hero-banner">
          <img class="sh-space-hero-image" src={coverUrl} alt="" />
        </div>
      )}
      <div class="sh-space-hero-body">
        <div class="sh-space-hero-identity">
          <span class="sh-space-hero-avatar" aria-hidden="true">
            {iconUrl ? <img class="sh-space-hero-avatar-img" src={iconUrl} alt="" /> : fallback}
          </span>
          <div class="sh-space-hero-meta">
            <h2 class="sh-space-hero-name">{name}</h2>
            {(countLabel || scope) && (
              <div class="sh-space-hero-line">
                {countLabel && (membersHref ? (
                  <a class="sh-space-hero-members" href={membersHref}>
                    {faces.length > 0 && (
                      <span class="sh-space-hero-faces" aria-hidden="true">
                        {faces.map((m) => (
                          <Avatar key={m.user_id} name={m.name} src={m.picture_url} size={24} />
                        ))}
                      </span>
                    )}
                    {countLabel}
                  </a>
                ) : (
                  <span class="sh-space-hero-members">{countLabel}</span>
                ))}
                {scope && (
                  <span class="sh-space-hero-scope">
                    {countLabel && <span aria-hidden="true">· </span>}{scope}
                  </span>
                )}
              </div>
            )}
          </div>
        </div>
        {about && (
          <>
            <div class="sh-space-hero-about" ref={aboutRef}>
              <MarkdownView src={about} />
            </div>
            {clamped && (
              <button type="button" class="sh-space-hero-more" aria-haspopup="dialog"
                      onClick={() => setAboutOpen(true)}>
                {t('space.hero.read_more')}
              </button>
            )}
          </>
        )}
        {links.length > 0 && (
          <nav class="sh-space-hero-links" aria-label={t('space.links.title')}>
            <ul>
              {links.map((link) => (
                <li key={link.id}>
                  <a class="sh-space-hero-link" href={link.href} target="_blank" rel="noopener noreferrer">
                    {LINK_ICON}
                    <span>{link.label}</span>
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        )}
      </div>
      {aboutOpen && about && (
        <Modal open onClose={() => setAboutOpen(false)}
               title={t('space.hero.about_title', { name })} class="sh-space-about-dialog">
          <div class="sh-space-about-dialog__body">
            <MarkdownView src={about} />
          </div>
        </Modal>
      )}
    </header>
  )
}
