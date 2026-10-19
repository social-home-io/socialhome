/**
 * SpaceSettingsPage — a space's settings on ONE page (spaces redesign,
 * PR 12; §23.91 / §23.123 / §23.124).
 *
 * Every section has its own address (``/spaces/:id/settings/:section``,
 * ``spaceSettingsSections``) and sits on one scrolling page, indexed by a
 * left rail on desktop and a row of jump chips on phones — real links
 * with ``aria-current``, not a tab widget. Opening a section's URL scrolls
 * to it; the URL survives Refresh and Back, and More's "Edit sections"
 * deep-links to ``…/settings/sections``. Links go through ``addBase`` so
 * they keep the HA ingress prefix.
 *
 * Save model (owner decision 8): only Basics is a form — its draft (text,
 * emoji, quick links, theme) is held here and saved with a page-level save bar ("1 unsaved change · Discard ·
 * Save") that appears only while something is unsaved; leaving the page
 * with a draft asks first (``navigationGuard`` + ``beforeunload``), moving
 * between sections doesn't. Every other switch, radio and select saves the
 * moment it changes, with a toast, as each section's line says.
 *
 * Who sees what (``settingsSections``): owners / admins get every section
 * (a remote admin — space hosted elsewhere — only what forwards to the
 * host); members and moderators their notifications, "You in this space",
 * their bots and Leave; followers their notifications and Unfollow.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation, useRoute } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { confirmDialog } from '@/components/confirm'
import { Spinner } from '@/components/Spinner'
import type { SpaceMemberSummary } from '@/components/SpaceSectionNav'
import { showToast } from '@/components/Toast'
import { useSpaceConfigWs } from '@/hooks/useSpaceConfigWs'
import { isOne, t } from '@/i18n/i18n'
import { setNavigationGuard, useBeforeUnload } from '@/router/navigationGuard'
import { currentUser } from '@/store/auth'
import { instanceConfig } from '@/store/instance'
import { setSpaceHereAllowed } from '@/store/spaceMembers'
import type { Space } from '@/types'
import { SpaceBotsTab } from './SpaceBotsTab'
import {
  SpaceNotificationChoices, useSpaceChatNotif, useSpaceNotifLevel,
} from './SpaceNotifLevels'
import { parseSpaceRole } from './spaceRoles'
import {
  legacySettingsSection, resolveSettingsSection, settingsSectionLabel, settingsSectionPath, settingsSections,
  type SettingsSection, type SettingsViewer,
} from './spaceSettingsSections'
import type { ThemeDraft } from '@/components/SpaceThemeStudio'
import { BasicsSection } from './settings/BasicsSection'
import { DangerSection } from './settings/DangerSection'
import { FeaturesSection } from './settings/FeaturesSection'
import { FindSection } from './settings/FindSection'
import { LocationSection } from './settings/LocationSection'
import { MembersSection } from './settings/MembersSection'
import { ModerationSection } from './settings/ModerationSection'
import { patchSpace } from './settings/spacePatch'
import { basicsOf, useBasicsForm } from './settings/useBasicsForm'
import type { LinksDraftHandle } from './SpaceLinksTab'
import {
  LeaveSection, UnfollowSection, YouSection, type MySeat,
} from './settings/YouSection'

interface SpaceDetail extends Space {
  about_markdown: string | null
  cover_url: string | null
  icon_url: string | null
  bot_enabled?: boolean
}

interface RosterRow extends MySeat {
  user_id: string
  role: string
}

/** The kind of viewer, from their seat and where the space is hosted. */
export function settingsViewer(role: string | undefined, isRemoteSpace: boolean): SettingsViewer | null {
  switch (parseSpaceRole(role)) {
    case 'owner':
    case 'admin': return isRemoteSpace ? 'remote-admin' : 'admin'
    case 'moderator':
    case 'member': return 'member'
    case 'subscriber': return 'follower'
    default: return null
  }
}

/** The line under a section's heading (how it saves), or ``null``. */
// For ``i18n:check``: t('space.settings_page.lead.basics') t('space.settings_page.lead.find')
// t('space.settings_page.lead.members') t('space.settings_page.lead.sections')
// t('space.settings_page.lead.notifications') t('space.settings_page.lead.location')
// t('space.settings_page.lead.moderation') t('space.settings_page.lead.you')
const LEADS: Partial<Record<SettingsSection, string>> = {
  basics: 'space.settings_page.lead.basics',
  find: 'space.settings_page.lead.find',
  members: 'space.settings_page.lead.members',
  sections: 'space.settings_page.lead.sections',
  notifications: 'space.settings_page.lead.notifications',
  location: 'space.settings_page.lead.location',
  moderation: 'space.settings_page.lead.moderation',
  you: 'space.settings_page.lead.you',
}

/** Rows that end the page as one collapsed row, titled by their summary. */
const COLLAPSED: readonly SettingsSection[] = ['danger', 'leave', 'unfollow']

const FALLBACK = { name: '', description: '', emoji: '', about_markdown: '' }

function scrollToSection(section: SettingsSection, focus: boolean) {
  const el = document.getElementById(`settings-${section}`)
  if (!el) return
  el.scrollIntoView?.({ block: 'start' })
  if (focus) (el.querySelector('[data-section-heading]') as HTMLElement | null)?.focus({ preventScroll: true })
}

export default function SpaceSettingsPage() {
  const { params } = useRoute()
  const { route, query } = useLocation()
  const spaceId = params.id
  const segment = params.section
  // An old link (a stored bell entry: ``/settings?household=…``) → its
  // section's own path, replacing the entry so Back skips the old form.
  const legacy = segment ? null : legacySettingsSection(query)
  useEffect(() => {
    if (legacy) route(settingsSectionPath(spaceId, legacy), true)
  }, [spaceId, legacy])
  const [space, setSpace] = useState<SpaceDetail | null>(null)
  const [roster, setRoster] = useState<RosterRow[]>([])
  const [loading, setLoading] = useState(true)
  const [sharing, setSharing] = useState<{ allowed: boolean; on: boolean }>({ allowed: false, on: false })

  /** ``quiet`` is the live-refresh path: a failed refetch keeps the page
   *  as it is instead of flipping to "Space not found". */
  const reload = async ({ quiet = false } = {}) => {
    try {
      const [detail, members] = await Promise.all([
        api.get(`/api/spaces/${spaceId}`) as Promise<SpaceDetail>,
        api.get(`/api/spaces/${spaceId}/members`) as Promise<RosterRow[]>,
      ])
      setSpace(detail)
      setRoster(Array.isArray(members) ? members : [])
      setSpaceHereAllowed(spaceId, detail.allow_here_mention === true)
    } catch {
      if (quiet) return
      setSpace(null)
      setRoster([])
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { setLoading(true); void reload() }, [spaceId])
  // Another admin renamed / reconfigured the space, or changed roles →
  // refresh; a dissolve leaves for the list.
  useSpaceConfigWs(spaceId, () => { void reload({ quiet: true }) })
  const refresh = () => { void reload({ quiet: true }) }

  const me = roster.find(m => m.user_id === currentUser.value?.user_id) ?? null
  const isRemoteSpace = !!(
    space?.owner_instance_id && instanceConfig.value?.instance_id
    && space.owner_instance_id !== instanceConfig.value.instance_id
  )
  // The detail's ``my_role`` is the viewer's seat (a follower's roster
  // doesn't list followers); the roster row backs it up.
  const myRole = space?.my_role ?? me?.role
  const viewer = space ? settingsViewer(myRole, isRemoteSpace) : null
  const isOwner = myRole === 'owner'
  const admin = viewer === 'admin' || viewer === 'remote-admin'
  const sections = viewer ? settingsSections(viewer) : []
  const active = viewer ? resolveSettingsSection(segment, viewer) : null

  const form = useBasicsForm(basicsOf(space ?? FALLBACK))
  // The theme studio inside Basics reports its draft here, so one save bar
  // covers the text and the theme.
  const [themeDraft, setThemeDraft] = useState<ThemeDraft | null>(null)
  // …and so does the quick-links editor (each added / removed / edited
  // link counts, a new order once).
  const [linksDraft, setLinksDraft] = useState<LinksDraftHandle | null>(null)
  const dirty = admin
    ? form.dirtyCount + (themeDraft?.dirty ? 1 : 0) + (linksDraft?.count ?? 0)
    : 0
  const dirtyRef = useRef(0)
  dirtyRef.current = dirty
  // The guard is registered once; it reads the latest drafts through this.
  const discardRef = useRef(() => {})
  discardRef.current = () => { form.reset(); themeDraft?.discard(); linksDraft?.discard() }
  const [saving, setSaving] = useState(false)

  const [notifLevel, setNotifLevel] = useSpaceNotifLevel(spaceId, viewer !== null)
  const chatOn = viewer !== null && viewer !== 'follower' && (space?.features?.chat ?? true)
  const chatNotif = useSpaceChatNotif(spaceId, chatOn)

  // A member's own location sharing (the switch lives on the Map).
  useEffect(() => {
    if (viewer !== 'member') return
    let stop = false
    api.get(`/api/spaces/${spaceId}/notif-prefs`)
      .then((b: { location_share_allowed?: boolean; location_share_enabled?: boolean }) => {
        if (!stop) setSharing({ allowed: b?.location_share_allowed === true, on: b?.location_share_enabled === true })
      })
      .catch(() => { /* the row just stays hidden */ })
    return () => { stop = true }
  }, [spaceId, viewer])

  // Leaving with an unsaved Basics draft asks first; moving between this
  // page's sections (its subpaths) doesn't.
  useEffect(() => setNavigationGuard({
    path: `/spaces/${spaceId}/settings`,
    subpaths: true,
    isDirty: () => dirtyRef.current > 0,
    confirm: async () => {
      const ok = await confirmDialog(t('space.settings_page.leave_body'), {
        title: t('settings.profile.leave_title'),
        confirmLabel: t('settings.profile.leave_discard'),
        cancelLabel: t('settings.profile.leave_stay'),
        destructive: true,
      })
      if (ok) discardRef.current()
      return ok
    },
  }), [spaceId])
  useBeforeUnload(dirty > 0)

  // Opening a section's URL (link, Back, a deep link) scrolls to it — and
  // again while the sections above it finish loading (links, theme,
  // notifications grow after the first paint), until the reader scrolls.
  const ready = !loading && active !== null
  useEffect(() => {
    if (!ready || !segment || !active) return
    let moved = false
    const stop = () => { moved = true }
    const opts = { passive: true } as const
    window.addEventListener('wheel', stop, opts)
    window.addEventListener('touchstart', stop, opts)
    window.addEventListener('keydown', stop)
    const go = () => { if (!moved) scrollToSection(active, false) }
    go()
    const timers = [setTimeout(go, 400), setTimeout(go, 1200)]
    return () => {
      timers.forEach(clearTimeout)
      window.removeEventListener('wheel', stop)
      window.removeEventListener('touchstart', stop)
      window.removeEventListener('keydown', stop)
    }
  }, [ready, segment, active])

  // Phones: keep the current jump chip in view in its sideways row.
  useEffect(() => {
    if (!ready || typeof window.matchMedia !== 'function'
      || !window.matchMedia('(max-width: 899px)').matches) return
    const chip = document.querySelector('.sh-ss-index a[aria-current="page"]') as HTMLElement | null
    chip?.scrollIntoView?.({ block: 'nearest', inline: 'center' })
  }, [ready, active])

  if (loading) return <Spinner />

  if (!space) {
    return (
      <div class="sh-empty-state">
        <h3>{t('space.settings_page.not_found')}</h3>
        <Button onClick={() => route(`/spaces/${spaceId}`)}>{t('common.back')}</Button>
      </div>
    )
  }
  if (!viewer || !active) {
    return (
      <div class="sh-empty-state">
        <div aria-hidden="true">🔒</div>
        <h3>{t('space.settings_page.members_only')}</h3>
        <p class="sh-muted">{t('space.settings_page.members_only_body')}</p>
        <Button onClick={() => route(`/spaces/${spaceId}`)}>{t('space.settings_page.back')}</Button>
      </div>
    )
  }

  const members: SpaceMemberSummary = {
    count: roster.filter(m => m.role !== 'subscriber').length,
    admins: roster.filter(m => m.role === 'owner' || m.role === 'admin').length,
  }

  const save = async () => {
    if (!form.fields.name.value.trim()) {
      showToast(t('space.settings_page.basics.name_required'), 'error')
      return
    }
    if (linksDraft && linksDraft.invalid > 0) {
      showToast(t('space.links.fix_first'), 'error')
      return
    }
    setSaving(true)
    let ok = true
    // One "Saved" for the whole bar: the text's PATCH says it itself.
    let said = form.dirtyCount > 0
    if (form.dirtyCount > 0) {
      const outcome = await patchSpace(space.id, form.changes, {
        okToast: t('space.settings.saved'), onUpdate: refresh,
      })
      // Forwarded: the host applies it; the draft would only linger.
      if (outcome === 'forwarded') form.reset()
      ok = outcome !== 'failed'
    }
    if (ok && themeDraft?.dirty) {
      if (await themeDraft.save() && !said) {
        showToast(t('space.settings.saved'), 'success')
        said = true
      }
    }
    // The links go last, one call at a time; a link whose call fails
    // stays in the draft (and the bar) and the editor names it.
    if (ok && linksDraft && linksDraft.count > 0) {
      if (await linksDraft.save() && !said) showToast(t('space.settings.saved'), 'success')
    }
    setSaving(false)
  }

  const body = (s: SettingsSection) => {
    switch (s) {
      case 'basics':
        return <BasicsSection space={space} form={form} isRemoteSpace={isRemoteSpace}
                              onUpdate={refresh} onThemeDraft={setThemeDraft}
                              onLinksDraft={setLinksDraft} />
      case 'find':
        return <FindSection space={space} isOwner={isOwner} isRemoteSpace={isRemoteSpace}
                            adminCount={members.admins} onUpdate={refresh} />
      case 'members':
        return <MembersSection space={space} isRemoteSpace={isRemoteSpace} members={members} onUpdate={refresh} />
      case 'sections':
        return <FeaturesSection space={space} onUpdate={refresh} />
      case 'notifications':
        return (
          <div class="sh-ss-card">
            {notifLevel ? (
              <SpaceNotificationChoices spaceId={space.id} level={notifLevel}
                                        onSaved={setNotifLevel} chatNotif={chatNotif} />
            ) : <p class="sh-muted">{t('common.loading')}</p>}
          </div>
        )
      case 'location':
        return <LocationSection space={space} onUpdate={refresh} />
      case 'moderation':
        return <ModerationSection space={space} isRemoteSpace={isRemoteSpace} onUpdate={refresh} />
      case 'bots':
        return (
          <div class="sh-ss-card">
            <SpaceBotsTab
              spaceId={space.id}
              // Space-scope bots are host-local; elsewhere an admin manages
              // only their own member-scope bots.
              canAdmin={viewer === 'admin'}
              currentUserId={currentUser.value?.user_id ?? null}
              botEnabled={space.bot_enabled === true}
              onBotEnabledChange={(next) => setSpace({ ...space, bot_enabled: next })}
            />
          </div>
        )
      case 'danger':
        return <DangerSection space={space} isOwner={isOwner} isRemoteSpace={isRemoteSpace}
                              open={segment === 'danger'} onUpdate={refresh} />
      case 'you':
        return <YouSection space={space} seat={me} locationAllowed={sharing.allowed} sharing={sharing.on} />
      case 'leave':
        return <LeaveSection space={space} />
      case 'unfollow':
        return <UnfollowSection space={space} />
    }
  }

  return (
    <div class="sh-space-settings-page">
      <header class="sh-ss-head">
        <a class="sh-ss-head__back" href={addBase(`/spaces/${spaceId}`)}>
          <span aria-hidden="true">‹</span> {space.name}
        </a>
        <h1 class="sh-ss-head__title">{t('nav.settings')}</h1>
      </header>
      <div class="sh-ss-layout">
        <nav class="sh-ss-index" aria-label={t('space.settings_page.index_aria')}>
          <ul>
            {sections.map(s => (
              <li key={s}>
                <a href={addBase(settingsSectionPath(space.id, s))}
                   class={'sh-ss-index__link'
                     + (COLLAPSED.includes(s) ? ' sh-ss-index__link--quiet' : '')}
                   aria-current={s === active ? 'page' : undefined}
                   // The router ignores a click on the URL it is already on;
                   // still take the reader there.
                   onClick={() => { if (s === active) scrollToSection(s, true) }}>
                  {settingsSectionLabel(s)}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div class="sh-ss-sections">
          {sections.map(s => (
            <section key={s} id={`settings-${s}`} class="sh-ss-section"
                     aria-labelledby={COLLAPSED.includes(s) ? undefined : `settings-${s}-h`}
                     aria-label={COLLAPSED.includes(s) ? settingsSectionLabel(s) : undefined}>
              {!COLLAPSED.includes(s) && (
                <>
                  <h2 id={`settings-${s}-h`} class="sh-ss-section__title"
                      tabIndex={-1} data-section-heading>
                    {settingsSectionLabel(s)}
                  </h2>
                  {LEADS[s] && <p class="sh-ss-section__lead">{t(LEADS[s]!)}</p>}
                </>
              )}
              {body(s)}
            </section>
          ))}
          {dirty > 0 && (
            <div class="sh-savebar sh-ss-savebar" role="region"
                 aria-label={t('settings.profile.unsaved')}>
              <span class="sh-savebar__text" role="status">
                {isOne(dirty)
                  ? t('space.settings_page.unsaved_one')
                  : t('space.settings_page.unsaved', { n: String(dirty) })}
              </span>
              <div class="sh-savebar__actions">
                <Button variant="secondary" onClick={() => discardRef.current()} disabled={saving}>
                  {t('settings.profile.discard')}
                </Button>
                <Button onClick={() => void save()} loading={saving}>{t('common.save')}</Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
