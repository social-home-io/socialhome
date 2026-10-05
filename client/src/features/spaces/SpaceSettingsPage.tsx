/**
 * SpaceSettingsPage — full space admin hub (§23.91 / §23.123 / §23.124).
 *
 * Tabs:
 *   - General: reuses :mod:`SpaceSettings` (name / emoji / join-mode +
 *     GFS federation + danger zone).
 *   - About: markdown editor + cover-image uploader.
 *   - Theme: :mod:`SpaceThemeStudio` rewrite with live preview.
 *   - Age & safety: :mod:`SpaceAgeGating` — min-age gate + category
 *     that blocks under-age minors from joining (§CP.F1).
 *
 * Only owners/admins may view; a non-member gets a 403 from the
 * detail endpoint and we render an access message.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { setSpaceHereAllowed } from '@/store/spaceMembers'
import { signal } from '@preact/signals'
import { useRoute, useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { MarkdownView } from '@/components/MarkdownView'
import { Spinner } from '@/components/Spinner'
import { SpaceSettings } from '@/components/SpaceSettings'
import { SpaceThemeStudio } from '@/components/SpaceThemeStudio'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { instanceConfig } from '@/store/instance'
import type { Space } from '@/types'
import { SpaceBotsTab } from './SpaceBotsTab'
import { SpaceLinksTab } from './SpaceLinksTab'
import { SpaceAgeGating } from '@/features/child-protection/SpaceAgeGating'
import { confirmDialog } from '@/components/confirm'
import { useSpaceConfigWs } from '@/hooks/useSpaceConfigWs'
import { t } from '@/i18n/i18n'
import { cssUrl } from '@/utils/cssUrl'

type SettingsTab = 'general' | 'about' | 'theme' | 'links' | 'age' | 'bots'

/**
 * Which settings tabs a viewer can see.
 *
 * - Non-admin members get only their own surface ("Bots").
 * - A local admin gets the full hub.
 * - A *remote* admin (the space is hosted on another household) gets only
 *   the tabs whose controls forward to the host — General (config / archive
 *   / dissolve + tier proposals all federate) and About. Theme, Quick links,
 *   and Age & safety are host-local config that would silently mutate our
 *   stub (the age gate is enforced by the host on join), so they're hidden
 *   on a remote stub.
 *
 * Exported pure so the gating is unit-tested without standing up a second
 * household.
 */
export function visibleSettingsTabs(
  canAdmin: boolean,
  isRemoteSpace: boolean,
): SettingsTab[] {
  if (!canAdmin) return ['bots']
  if (isRemoteSpace) return ['general', 'about', 'bots']
  return ['general', 'about', 'theme', 'links', 'age', 'bots']
}

interface SpaceDetail extends Space {
  about_markdown: string | null
  cover_url: string | null
  cover_hash: string | null
  icon_url: string | null
  icon_hash: string | null
  bot_enabled?: boolean
}

const activeTab = signal<SettingsTab>('general')

export default function SpaceSettingsPage() {
  const { params } = useRoute()
  const { route } = useLocation()
  const spaceId = params.id
  const [space, setSpace] = useState<SpaceDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [canAdmin, setCanAdmin] = useState(false)
  // Owner-only settings (e.g. how members publish over a connection server)
  // render only for the space owner — the server refuses anyone else.
  const [isOwner, setIsOwner] = useState(false)
  const [isMember, setIsMember] = useState(false)

  /** ``quiet`` is the live-refresh path: a failed refetch keeps the
   *  page as it is instead of flipping to "Space not found". */
  const reload = async ({ quiet = false } = {}) => {
    try {
      const [detail, members] = await Promise.all([
        api.get(`/api/spaces/${spaceId}`) as Promise<SpaceDetail>,
        api.get(`/api/spaces/${spaceId}/members`) as Promise<
          Array<{ user_id: string; role: string }>
        >,
      ])
      setSpace(detail)
      setSpaceHereAllowed(spaceId, detail.allow_here_mention === true)
      const mine = members.find(
        m => m.user_id === currentUser.value?.user_id,
      )
      setCanAdmin(mine?.role === 'owner' || mine?.role === 'admin')
      setIsOwner(mine?.role === 'owner')
      setIsMember(Boolean(mine))
    } catch {
      if (quiet) return
      setSpace(null)
      setCanAdmin(false)
      setIsOwner(false)
      setIsMember(false)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void reload() }, [spaceId])
  // Another admin renamed / reconfigured the space, or changed roles →
  // refresh the detail + tab gating; a dissolve leaves for the list.
  useSpaceConfigWs(spaceId, () => { void reload({ quiet: true }) })

  if (loading) return <Spinner />

  if (!space) {
    return (
      <div class="sh-empty-state">
        <h3>{t('space.settings_page.not_found')}</h3>
        <Button onClick={() => route(`/spaces/${spaceId}`)}>{t('common.back')}</Button>
      </div>
    )
  }

  // Any space member can reach the "Bots" tab to manage their OWN
  // personal bots. Admin-only guards for the shared space-scope bots
  // live inside SpaceBotsTab. Non-members still get the 403 screen.
  if (!isMember) {
    return (
      <div class="sh-empty-state">
        <div aria-hidden="true">🔒</div>
        <h3>{t('space.settings_page.members_only')}</h3>
        <p class="sh-muted">{t('space.settings_page.members_only_body')}</p>
        <Button onClick={() => route(`/spaces/${spaceId}`)}>
          {t('space.settings_page.back')}
        </Button>
      </div>
    )
  }

  // A stub of a space hosted on another household: General config, archive,
  // and the dissolve / tier proposals all forward to the host, but theme /
  // links / GFS publication are host-local — see visibleSettingsTabs.
  const isRemoteSpace = !!(
    space.owner_instance_id &&
    instanceConfig.value?.instance_id &&
    space.owner_instance_id !== instanceConfig.value.instance_id
  )
  // Per-member notifications live on the 🔔 bell in the space header, not
  // here — this page has no Notifications tab.
  const visibleTabs = visibleSettingsTabs(canAdmin, isRemoteSpace)
  if (!visibleTabs.includes(activeTab.value)) {
    activeTab.value = visibleTabs[0]
  }

  const tabLabel = (tab: SettingsTab): string => {
    switch (tab) {
      case 'general':       return t('space.settings_page.tab_general')
      case 'about':         return t('space.settings_page.tab_about')
      case 'theme':         return t('space.settings_page.tab_theme')
      case 'links':         return t('space.settings_page.tab_links')
      case 'age':           return t('space.settings_page.tab_age')
      case 'bots':          return t('space.settings_page.tab_bots')
    }
  }

  return (
    <div class="sh-space-settings-page">
      <div class="sh-page-header">
        <h1>⚙ {t('space.settings_page.title', { name: space.name })}</h1>
        <Button variant="secondary"
                onClick={() => route(`/spaces/${spaceId}`)}>
          ← {t('space.settings_page.back')}
        </Button>
      </div>

      <nav class="sh-space-tabs" role="tablist">
        {visibleTabs.map(tab => (
          <button key={tab} type="button" role="tab"
                  aria-selected={activeTab.value === tab}
                  class={activeTab.value === tab ? 'sh-tab sh-tab--active' : 'sh-tab'}
                  onClick={() => { activeTab.value = tab }}>
            {tabLabel(tab)}
          </button>
        ))}
      </nav>

      {/* Member-only context line — non-admins see just the Bots tab and
       *  might wonder why the rest are missing.  Naming the scope explicitly
       *  makes the limited surface read as "your settings for this space"
       *  rather than as a permissions glitch, and points them at the 🔔 bell
       *  in the space header for their notification level. */}
      {!canAdmin && (
        <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-sm)' }}>
          {t('space.settings_page.member_note')}
        </p>
      )}

      {activeTab.value === 'general' && (
        <SpaceSettings
          space={space}
          onUpdate={() => void reload()}
          isRemoteSpace={isRemoteSpace}
          isOwner={isOwner}
        />
      )}
      {activeTab.value === 'about' && (
        <AboutTab space={space} onSaved={() => void reload()} />
      )}
      {activeTab.value === 'theme' && (
        <SpaceThemeStudio spaceId={space.id} />
      )}
      {activeTab.value === 'links' && (
        <SpaceLinksTab spaceId={space.id} />
      )}
      {activeTab.value === 'age' && (
        <SpaceAgeGating spaceId={space.id} />
      )}
      {activeTab.value === 'bots' && (
        <SpaceBotsTab
          spaceId={space.id}
          // Space-scope bot management is host-local; on a remote stub a
          // remote admin manages only their own member-scope bots.
          canAdmin={canAdmin && !isRemoteSpace}
          currentUserId={currentUser.value?.user_id ?? null}
          botEnabled={space.bot_enabled === true}
          onBotEnabledChange={(next) => setSpace({ ...space, bot_enabled: next })}
        />
      )}
    </div>
  )
}

function AboutTab({
  space, onSaved,
}: { space: SpaceDetail; onSaved: () => void }) {
  const [markdown, setMarkdown] = useState(space.about_markdown ?? '')
  const [saving, setSaving] = useState(false)
  const [uploadingCover, setUploadingCover] = useState(false)
  const [coverUrl, setCoverUrl] = useState<string | null>(space.cover_url)
  const [uploadingIcon, setUploadingIcon] = useState(false)
  const [iconUrl, setIconUrl] = useState<string | null>(space.icon_url)
  const fileRef = useRef<HTMLInputElement | null>(null)

  const uploadIcon = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    setUploadingIcon(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const resp = (await api.upload(`/api/spaces/${space.id}/icon`, fd)) as {
        icon_url: string
      }
      setIconUrl(resp.icon_url)
      onSaved()
      showToast(t('space.about.icon_updated'), 'success')
    } catch (err: unknown) {
      showToast(t('space.about.upload_failed', { error: String((err as Error).message ?? err) }), 'error')
    } finally {
      setUploadingIcon(false)
      input.value = ''
    }
  }

  const clearIcon = async () => {
    if (!(await confirmDialog(t('space.about.icon_remove_confirm'), { destructive: true }))) return
    try {
      await api.delete(`/api/spaces/${space.id}/icon`)
      setIconUrl(null)
      onSaved()
      showToast(t('space.about.icon_removed'), 'info')
    } catch (err: unknown) {
      showToast(t('space.about.remove_failed', { error: String((err as Error).message ?? err) }), 'error')
    }
  }

  const saveAbout = async () => {
    setSaving(true)
    try {
      await api.patch(`/api/spaces/${space.id}`, {
        about_markdown: markdown,
      })
      showToast(t('space.about.saved'), 'success')
      onSaved()
    } catch (err: unknown) {
      showToast(
        t('space.about.save_failed', { error: String((err as Error).message ?? err) }), 'error',
      )
    } finally {
      setSaving(false)
    }
  }

  const uploadCover = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    setUploadingCover(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const resp = await api.upload(
        `/api/spaces/${space.id}/cover`, fd,
      ) as { cover_url: string }
      setCoverUrl(resp.cover_url)
      onSaved()
      showToast(t('space.about.cover_updated'), 'success')
    } catch (err: unknown) {
      showToast(
        t('space.about.upload_failed', { error: String((err as Error).message ?? err) }), 'error',
      )
    } finally {
      setUploadingCover(false)
      input.value = ''
    }
  }

  const clearCover = async () => {
    if (!await confirmDialog(t('space.about.cover_remove_confirm'), { destructive: true })) return
    try {
      await api.delete(`/api/spaces/${space.id}/cover`)
      setCoverUrl(null)
      onSaved()
      showToast(t('space.about.cover_removed'), 'info')
    } catch (err: unknown) {
      showToast(
        t('space.about.remove_failed', { error: String((err as Error).message ?? err) }), 'error',
      )
    }
  }

  return (
    <div class="sh-form sh-about-editor">
      <section>
        <h3 style={{ margin: 0 }}>{t('space.about.cover_title')}</h3>
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
          {t('space.about.cover_hint')}
        </p>
        <div class="sh-about-cover-preview"
             style={coverUrl ? { backgroundImage: cssUrl(coverUrl) } : {}}>
          {!coverUrl && (
            <span class="sh-muted">{t('space.about.cover_empty')}</span>
          )}
        </div>
        <div class="sh-row" style={{ gap: 'var(--sh-space-xs)', flexWrap: 'wrap' }}>
          <label class="sh-btn sh-btn--secondary">
            {coverUrl ? t('space.about.cover_change') : t('space.about.cover_upload')}
            <input ref={fileRef} type="file" accept="image/*"
                   class="sr-only" onChange={uploadCover} />
          </label>
          {uploadingCover && <span class="sh-muted">{t('space.about.uploading')}</span>}
          {coverUrl && (
            <Button variant="secondary" onClick={clearCover}>
              {t('space.about.remove')}
            </Button>
          )}
        </div>
      </section>

      <section>
        <h3 style={{ margin: 0 }}>{t('space.about.icon_title')}</h3>
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
          {t('space.about.icon_hint')}
        </p>
        <div class="sh-row" style={{ gap: 'var(--sh-space-sm)', alignItems: 'center' }}>
          <span
            class="sh-about-icon-preview"
            style={iconUrl ? { backgroundImage: cssUrl(iconUrl) } : {}}
          >
            {!iconUrl && (space.emoji || '🏠')}
          </span>
          <label class="sh-btn sh-btn--secondary">
            {iconUrl ? t('space.about.icon_change') : t('space.about.icon_upload')}
            <input type="file" accept="image/*" class="sr-only" onChange={uploadIcon} />
          </label>
          {uploadingIcon && <span class="sh-muted">{t('space.about.uploading')}</span>}
          {iconUrl && (
            <Button variant="secondary" onClick={clearIcon}>
              {t('space.about.remove')}
            </Button>
          )}
        </div>
      </section>

      <section>
        <h3 style={{ margin: 0 }}>{t('space.about.text_title')}</h3>
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
          {t('space.about.text_hint')}
        </p>
        <div class="sh-about-editor-grid">
          <label class="sh-about-editor-pane">
            <span class="sh-muted">{t('space.about.write')}</span>
            <textarea class="sh-about-editor-textarea"
                      value={markdown}
                      rows={12} maxLength={8000}
                      placeholder={t('space.about.placeholder')}
                      onInput={(e) =>
                        setMarkdown((e.target as HTMLTextAreaElement).value)} />
            <span class="sh-char-count">
              {markdown.length} / 8000
            </span>
          </label>
          <div class="sh-about-editor-pane">
            <span class="sh-muted">{t('theme.preview')}</span>
            <div class="sh-about-editor-preview">
              {markdown.trim()
                ? <MarkdownView src={markdown} live />
                : <span class="sh-muted">{t('space.about.preview_empty')}</span>}
            </div>
          </div>
        </div>
        <div class="sh-form-actions">
          <Button onClick={saveAbout} loading={saving}>{t('space.about.save')}</Button>
        </div>
      </section>
    </div>
  )
}
