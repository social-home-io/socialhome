/**
 * TopBarHeading — the page title in the TopBar, with the optional face
 * a person-focused page sets (:func:`useTitleAvatar`).
 *
 * When that page names a person (a 1:1 DM's peer, ``personId``), the
 * title is a button that opens their person sheet and the face is its
 * tappable twin (one tab stop, not two). Every other page renders a
 * plain ``h1``.
 */
import { pageTitle, pageTitleAvatar } from '@/store/pageTitle'
import { Avatar } from './Avatar'
import { PersonButton } from './PersonButton'

export function TopBarHeading() {
  const title = pageTitle.value
  if (!title) return null
  const avatar = pageTitleAvatar.value
  const personId = avatar?.personId
  const face = avatar && (
    <Avatar src={avatar.src} name={avatar.name || title} size={28} />
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
