/**
 * TopBarHeading — the page title in the TopBar, with the optional face
 * a person-focused page sets (:func:`useTitleAvatar`).
 *
 * When that page names a person (a 1:1 DM's peer, ``personId``), the
 * title is a button that opens their person sheet and the face is its
 * tappable twin (one tab stop, not two). Every other page renders a
 * plain ``h1``. A space page sets its symbol instead
 * (:func:`useTitleGlyph`): a small tile with the space's picture, else
 * its Home Assistant logo / emoji, else its initial.
 */
import { pageTitle, pageTitleAvatar, pageTitleGlyph } from '@/store/pageTitle'
import { SpaceGlyph } from '@/features/spaces/SpaceGlyph'
import { Avatar } from './Avatar'
import { PersonButton } from './PersonButton'

export function TopBarHeading() {
  const title = pageTitle.value
  if (!title) return null
  const avatar = pageTitleAvatar.value
  const personId = avatar?.personId
  const glyph = pageTitleGlyph.value
  const face = avatar ? (
    <Avatar src={avatar.src} name={avatar.name || title} size={28} />
  ) : glyph && (
    <span class="sh-topbar-glyph" aria-hidden="true" data-testid="topbar-glyph">
      <SpaceGlyph iconUrl={glyph.iconUrl} emoji={glyph.emoji} name={glyph.name} />
    </span>
  )
  return (
    <div class="sh-topbar-heading">
      {personId ? (
        <PersonButton userId={personId} hint={avatar?.personHint}
          class="sh-person-btn--avatar" decorative>
          {face}
        </PersonButton>
      ) : face}
      <h1 class="sh-topbar-title">
        {personId ? (
          <PersonButton userId={personId} hint={avatar?.personHint}
            class="sh-person-btn--name sh-topbar-person"
            data-testid="topbar-person">
            {title}
          </PersonButton>
        ) : title}
      </h1>
    </div>
  )
}
