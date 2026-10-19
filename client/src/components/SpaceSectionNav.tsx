/**
 * SpaceSectionNav — the sticky row under the top bar on a space page:
 * the space's sections as real links, a More disclosure, and the gear.
 *
 * - **Links, not tabs.** Each section has its own URL
 *   (``/spaces/:id/:section``), so the row is a ``<nav>`` of ``<a>``s
 *   with ``aria-current="page"`` on the open one: one Tab stop per link,
 *   Enter follows it, Back and Refresh keep the section.
 * - **Priority+.** One fixed order (``SECTION_ORDER``); as many sections
 *   as fit show inline, the rest move into More from the end. Measured
 *   with ResizeObserver (``usePriorityPlus``); never reordered by
 *   activity. A phone shows Feed · Chat · Calendar · Tasks · More, a
 *   desktop most of them.
 * - **More** is a disclosure button (``aria-expanded`` /
 *   ``aria-controls``). Desktop: a non-modal popover under it (Esc or a
 *   click outside closes it, focus returns to the button). Phone: the
 *   shared Modal's bottom sheet (``role=dialog``, ``aria-modal``,
 *   heading, ✕, focus trap, Esc). Rows: Members first, then the sections
 *   that didn't fit — each with what it holds ("5 open · 2 yours", read
 *   from ``/summary`` when More opens) — Moderation (with the needs-you
 *   count) and "Your notifications" — the space's notification level,
 *   which replaced the per-space bell (the app keeps one bell). The
 *   footer: "Edit sections" for admins, "Leave space" for a member who
 *   isn't the owner (the owner reads why they can't leave yet).
 * - **Two badge kinds.** A quiet grey count is "unread" (Chat); a filled
 *   primary count is "needs you" (Moderation, rolled up onto More).
 *   Screen readers hear "Chat, 4 unread" / "More, 3 need you".
 * - **Gear** (Settings) stays one tap on every viewport — for the roles
 *   that may open settings (the host decides; followers never get it).
 */
import type { JSX } from 'preact'
import { useEffect, useId, useRef, useState } from 'preact/hooks'
import { addBase } from '@/baseUrl'
import { isOne, t } from '@/i18n/i18n'
import { Modal } from './Modal'
import { SideNavIcon, type SideNavIconName } from './SideNavIcon'
import { usePriorityPlus } from './priorityPlus'
import {
  sectionLabel, spaceSectionPath,
  type SpaceSection, type SpaceTabFeatures,
} from '@/features/spaces/spaceTabs'
import { sectionMeta, useSpaceSummary } from '@/features/spaces/spaceSummary'
import { MuteSection } from '@/features/dms/ConversationMute'
import type { ConversationNotifLevel } from '@/store/dms'
import { isMuteActive } from '@/utils/mute'
import {
  SpaceNotifLevelPicker, notifLevelLabel, type NotifLevel,
} from '@/features/spaces/SpaceNotifLevels'

/** Phones get the bottom sheet; matches the Modal's sheet breakpoint. */
const PHONE_QUERY = '(max-width: 640px)'

function isPhone(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia(PHONE_QUERY).matches
}

export interface SpaceMemberSummary {
  count: number
  admins: number
}

/** The viewer's space-chat notifications, set next to the space's own
 *  level in "Your notifications" (one place — the chat has no bell of
 *  its own). */
export interface SpaceChatNotif {
  convId: string
  mutedUntil: string | null
  level: ConversationNotifLevel
  onMute: (mutedUntil: string | null) => void
  onLevel: (level: ConversationNotifLevel) => void
}

export interface SpaceNeedsYou {
  review: number
  reports: number
  joinRequests: number
}

interface Props {
  spaceId: string
  spaceName: string
  /** The bar's sections, in ``SECTION_ORDER`` (no Members / Moderation). */
  sections: readonly SpaceSection[]
  /** The open section (may be one that lives in More). */
  active: SpaceSection
  features?: SpaceTabFeatures
  /** Unread space-chat messages (quiet badge on Chat); 0 → none. */
  chatUnread: number
  members: SpaceMemberSummary | null
  /** Content authority: show the Moderation row. ``null`` → no row. */
  needsYou: SpaceNeedsYou | null
  /** The viewer's notification level here; ``null`` → no row. */
  notifLevel: NotifLevel | null
  onNotifLevel: (level: NotifLevel) => void
  /** The space chat's notifications (a member with the chat open to
   *  them); ``null`` → the posts level alone. */
  chatNotif?: SpaceChatNotif | null
  /** Settings link for writer roles; ``null`` hides the gear. */
  settingsHref: string | null
  /** Settings authority: the "Edit sections" footer in More. */
  canEditSections: boolean
  /** "Leave space" in More's footer: ``'member'`` may leave (asks to
   *  confirm, then ``onLeave``); ``'owner'`` reads why not yet; ``null``
   *  (a follower — they unfollow from the feed — or unknown) shows
   *  nothing. */
  leave?: 'member' | 'owner' | null
  onLeave?: () => void
}

const SECTION_ICON: Partial<Record<SpaceSection, SideNavIconName>> = {
  feed: 'feed',
  chat: 'messages',
  calendar: 'calendar',
  tasks: 'tasks',
  pages: 'pages',
  gallery: 'gallery',
  stickies: 'stickies',
  bazaar: 'bazaar',
  members: 'presence',
}

const SVG = {
  width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  'aria-hidden': 'true', focusable: 'false',
} as const

const EXTRA_ICONS: Record<'map' | 'moderation' | 'bell' | 'gear' | 'chevron' | 'down', JSX.Element> = {
  map: (
    <svg {...SVG}><path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2z" /><path d="M9 4v14M15 6v14" /></svg>
  ),
  moderation: (
    <svg {...SVG}><path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z" /></svg>
  ),
  bell: (
    <svg {...SVG}><path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z" /><path d="M10 21h4" /></svg>
  ),
  gear: (
    <svg {...SVG}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  ),
  chevron: (<svg {...SVG} class="sh-flip-rtl"><path d="M9 6l6 6-6 6" /></svg>),
  down: (<svg {...SVG} width={16} height={16}><path d="M6 9l6 6 6-6" /></svg>),
}

function SectionIcon({ section }: { section: SpaceSection }) {
  const name = SECTION_ICON[section]
  if (name) return <SideNavIcon name={name} />
  return <span class="sh-sidenav-icon">{section === 'map' ? EXTRA_ICONS.map : EXTRA_ICONS.moderation}</span>
}

/** Quiet grey "unread" count — visible number, spoken as ", N unread". */
function UnreadBadge({ count }: { count: number }) {
  if (count <= 0) return null
  return (
    <>
      <span class="sh-space-badge sh-space-badge--unread" aria-hidden="true">{count > 99 ? '99+' : count}</span>
      <span class="sr-only">{', '}{t('space.sections.unread', { count: String(count) })}</span>
    </>
  )
}

/** Filled primary "needs you" count — spoken as ", N need you". */
function NeedsYouBadge({ count }: { count: number }) {
  if (count <= 0) return null
  return (
    <>
      <span class="sh-space-badge sh-space-badge--needs" aria-hidden="true">{count > 99 ? '99+' : count}</span>
      <span class="sr-only">{', '}{t(isOne(count) ? 'space.sections.needs_you_one' : 'space.sections.needs_you', { count: String(count) })}</span>
    </>
  )
}

function needsYouTotal(n: SpaceNeedsYou | null): number {
  return n ? n.review + n.reports + n.joinRequests : 0
}

/** "2 posts to review · 1 join request · 1 report" — or "Nothing waiting". */
function needsYouMeta(n: SpaceNeedsYou): string {
  const parts: string[] = []
  if (n.review > 0) parts.push(t(isOne(n.review) ? 'space.more.review_one' : 'space.more.review', { count: String(n.review) }))
  if (n.joinRequests > 0) parts.push(t(isOne(n.joinRequests) ? 'space.more.join_one' : 'space.more.join', { count: String(n.joinRequests) }))
  if (n.reports > 0) parts.push(t(isOne(n.reports) ? 'space.more.reports_one' : 'space.more.reports', { count: String(n.reports) }))
  return parts.length ? parts.join(' · ') : t('space.more.nothing_waiting')
}

function membersMeta(m: SpaceMemberSummary): string {
  const people = t(isOne(m.count) ? 'space.more.people_one' : 'space.more.people', { count: String(m.count) })
  if (m.admins <= 0) return people
  const admins = t(isOne(m.admins) ? 'space.more.admins_one' : 'space.more.admins', { count: String(m.admins) })
  return `${people} · ${admins}`
}

export function SpaceSectionNav({
  spaceId, spaceName, sections, active, features, chatUnread,
  members, needsYou, notifLevel, onNotifLevel, chatNotif = null,
  settingsHref, canEditSections, leave = null, onLeave,
}: Props) {
  const rowRef = useRef<HTMLUListElement>(null)
  const measureRef = useRef<HTMLDivElement>(null)
  const moreBtnRef = useRef<HTMLButtonElement>(null)
  const wrapRef = useRef<HTMLLIElement>(null)
  const panelId = useId()
  const [open, setOpen] = useState(false)
  // The sheet on a phone, the popover elsewhere — decided when opened.
  const [sheet, setSheet] = useState(false)
  // The panel's view: the rows, or the notification level choice.
  const [view, setView] = useState<'rows' | 'notif'>('rows')
  // What the hidden sections hold — read each time More opens.
  const summary = useSpaceSummary(spaceId, open)

  const label = (s: SpaceSection) => sectionLabel(s, features)
  const badgeOf = (s: SpaceSection) => (s === 'chat' && active !== 'chat' ? chatUnread : 0)
  const needs = needsYouTotal(needsYou)
  const fitKey = [
    sections.map(label).join('\u0000'), chatUnread > 0 ? String(chatUnread) : '',
    needs > 0 ? String(needs) : '',
  ].join('|')
  const shown = usePriorityPlus(rowRef, measureRef, sections.length, fitKey)
  const inline = sections.slice(0, shown)
  const overflow = sections.slice(shown)
  const activeInMore = !inline.includes(active)

  // Closing the panel resets it to its rows.
  const close = (refocus: boolean) => {
    setOpen(false)
    setView('rows')
    if (refocus) moreBtnRef.current?.focus()
  }

  // The desktop popover: Esc closes and returns focus, a click outside
  // closes. (The sheet is a Modal, which does both itself.)
  useEffect(() => {
    if (!open || sheet) return
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) close(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); close(true) }
    }
    // Tabbing past the last row leaves the popover: close it rather than
    // leave it covering the page the focus moved on to.
    const onFocus = (e: FocusEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) close(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    document.addEventListener('focusin', onFocus)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('focusin', onFocus)
    }
  }, [open, sheet])

  // Moving between the rows and the level choice keeps focus inside the
  // panel: on the chosen level going in, on the row coming back.
  const panelRef = useRef<HTMLDivElement>(null)
  const firstView = useRef(true)
  useEffect(() => {
    if (firstView.current) { firstView.current = false; return }
    if (!open) return
    const root = panelRef.current
    const target = view === 'notif'
      ? root?.querySelector<HTMLElement>('input:checked, input')
      : root?.querySelector<HTMLElement>('[data-row="notifications"]')
    target?.focus()
  }, [view]) // eslint-disable-line react-hooks/exhaustive-deps

  // A section change (a row followed) closes the panel.
  useEffect(() => { if (open) close(false) }, [active, spaceId]) // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = () => {
    if (open) { close(false); return }
    setSheet(isPhone())
    setView('rows')
    setOpen(true)
  }

  const sectionLink = (s: SpaceSection, extraClass: string, children: preact.ComponentChildren) => (
    <a
      href={addBase(spaceSectionPath(spaceId, s))}
      class={active === s ? `${extraClass} ${extraClass}--active` : extraClass}
      aria-current={active === s ? 'page' : undefined}
      data-section={s}
    >
      {children}
    </a>
  )

  const row = (
    key: string, icon: JSX.Element, title: string, meta: string | null,
    badge: number, link: string | null, current: boolean, onClick?: () => void,
  ) => {
    const body = (
      <>
        <span class="sh-space-more__icon" aria-hidden="true">{icon}</span>
        <span class="sh-space-more__text">
          <span class="sh-space-more__title">{title}</span>
          {meta && <span class="sh-space-more__meta">{meta}</span>}
        </span>
        <NeedsYouBadge count={badge} />
        <span class="sh-space-more__chevron" aria-hidden="true">{EXTRA_ICONS.chevron}</span>
      </>
    )
    return (
      <li key={key}>
        {link ? (
          <a href={addBase(link)} class={current ? 'sh-space-more__row sh-space-more__row--active' : 'sh-space-more__row'}
             aria-current={current ? 'page' : undefined} data-section={key}>
            {body}
          </a>
        ) : (
          <button type="button" class="sh-space-more__row" onClick={onClick} data-row={key}>{body}</button>
        )}
      </li>
    )
  }

  const rows = (
    <>
      <ul class="sh-space-more__list">
        {row('members', <SectionIcon section="members" />, label('members'),
          members ? membersMeta(members) : null, 0,
          spaceSectionPath(spaceId, 'members'), active === 'members')}
        {overflow.map((s) => row(s, <SectionIcon section={s} />, label(s), sectionMeta(s, summary), 0,
          spaceSectionPath(spaceId, s), active === s))}
        {needsYou && row('moderation', <SectionIcon section="moderation" />, label('moderation'),
          needsYouMeta(needsYou), needs, spaceSectionPath(spaceId, 'moderation'),
          active === 'moderation')}
        {notifLevel && row('notifications', <span class="sh-sidenav-icon">{EXTRA_ICONS.bell}</span>,
          t('space.more.notifications'),
          chatNotif && isMuteActive(chatNotif.mutedUntil)
            ? `${notifLevelLabel(notifLevel)} · ${t('space.more.chat_muted')}`
            : notifLevelLabel(notifLevel),
          0, null, false,
          () => setView('notif'))}
      </ul>
      {canEditSections && settingsHref && (
        <p class="sh-space-more__footer">
          <span>{t('space.more.edit_hint')}</span>{' '}
          <a href={settingsHref}>{t('space.more.edit_sections')}</a>
        </p>
      )}
      {leave === 'member' && onLeave && (
        <div class="sh-space-more__leave-row">
          <button type="button" class="sh-space-more__leave" data-row="leave"
                  onClick={() => { close(true); onLeave() }}>
            {t('space.leave.button')}
          </button>
        </div>
      )}
      {leave === 'owner' && (
        <p class="sh-space-more__footer sh-space-more__owner-note">{t('space.leave.owner_note')}</p>
      )}
    </>
  )

  const notifView = notifLevel && (
    <div class="sh-space-more__notif">
      <button type="button" class="sh-space-more__back" onClick={() => setView('rows')}>
        <span aria-hidden="true" class="sh-flip-rtl">‹</span> {t('space.more.back')}
      </button>
      {!sheet && <h2 class="sh-space-more__heading">{t('space.more.notifications')}</h2>}
      {chatNotif && <h3 class="sh-space-more__subheading">{t('space.more.notif_posts')}</h3>}
      <SpaceNotifLevelPicker
        spaceId={spaceId}
        level={notifLevel}
        // With the chat's choice below, stay so both can be set.
        onSaved={(l) => { onNotifLevel(l); if (!chatNotif) setView('rows') }}
      />
      {chatNotif && (
        <MuteSection
          convId={chatNotif.convId}
          mutedUntil={chatNotif.mutedUntil}
          onChange={chatNotif.onMute}
          level={chatNotif.level}
          onLevelChange={chatNotif.onLevel}
          heading={t('space.more.notif_chat')}
        />
      )}
    </div>
  )

  const panelBody = view === 'notif' ? notifView : rows

  return (
    <>
      <div class="sh-space-subheader sh-space-subheader--sections">
        <nav class="sh-space-sections" aria-label={t('space.header.sections')}>
          <ul class="sh-space-sections__row" ref={rowRef}>
            {inline.map((s) => (
              <li key={s}>
                {sectionLink(s, 'sh-space-section', (
                  <>
                    <span class="sh-space-section__label">{label(s)}</span>
                    <UnreadBadge count={badgeOf(s)} />
                  </>
                ))}
              </li>
            ))}
            <li class="sh-space-more" ref={wrapRef}>
              <button
                ref={moreBtnRef}
                type="button"
                class={activeInMore ? 'sh-space-section sh-space-section--active sh-space-more__btn' : 'sh-space-section sh-space-more__btn'}
                aria-expanded={open}
                aria-controls={panelId}
                onClick={toggle}
              >
                <span class="sh-space-section__label">{t('space.more.button')}</span>
                {activeInMore && (
                  <span class="sr-only">{', '}{t('space.more.current', { section: label(active) })}</span>
                )}
                <NeedsYouBadge count={needs} />
                <span class="sh-space-more__down" aria-hidden="true">{EXTRA_ICONS.down}</span>
              </button>
              {open && !sheet && (
                <div id={panelId} ref={panelRef} class="sh-space-more__popover" role="region"
                     aria-label={t('space.more.title', { name: spaceName })}>
                  {panelBody}
                </div>
              )}
            </li>
          </ul>
          {/* An unconstrained, invisible copy of every section and of More:
           *  their widths decide how many fit (``usePriorityPlus``). */}
          <div class="sh-space-sections__measure" ref={measureRef} aria-hidden="true">
            {sections.map((s) => (
              <span key={s} class="sh-space-section">
                <span class="sh-space-section__label">{label(s)}</span>
                {badgeOf(s) > 0 && <span class="sh-space-badge sh-space-badge--unread">{badgeOf(s)}</span>}
              </span>
            ))}
            <span class="sh-space-section sh-space-more__btn">
              <span class="sh-space-section__label">{t('space.more.button')}</span>
              {needs > 0 && <span class="sh-space-badge sh-space-badge--needs">{needs}</span>}
              <span class="sh-space-more__down">{EXTRA_ICONS.down}</span>
            </span>
          </div>
        </nav>
        {settingsHref && (
          <a href={settingsHref} class="sh-space-gear" aria-label={t('space.header.settings_aria')}
             title={t('nav.settings')}>
            {EXTRA_ICONS.gear}
          </a>
        )}
      </div>
      {/* The sheet sits outside the sticky row: the row is a stacking
       *  context (z-index 50) that would keep the sheet under the phone's
       *  bottom nav. */}
      {open && sheet && (
        <Modal
          open
          onClose={() => close(false)}
          title={view === 'notif' ? t('space.more.notifications') : t('space.more.title', { name: spaceName })}
          class="sh-space-more__sheet"
        >
          <div id={panelId} ref={panelRef}>{panelBody}</div>
        </Modal>
      )}
    </>
  )
}
