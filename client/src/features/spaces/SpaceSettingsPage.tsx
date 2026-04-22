/**
 * SpaceSettingsPage — full space admin hub (§23.91 / §23.123 / §23.124).
 *
 * Tabs:
 *   - General: reuses :mod:`SpaceSettings` (name / emoji / join-mode +
 *     GFS federation + danger zone).
 *   - About: markdown editor + cover-image uploader.
 *   - Theme: :mod:`SpaceThemeStudio` rewrite with live preview.
 *
 * Only owners/admins may view; a non-member gets a 403 from the
 * detail endpoint and we render an access message.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
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
import type { Space } from '@/types'
import { SpaceBotsTab } from './SpaceBotsTab'

type SettingsTab = 'general' | 'about' | 'theme' | 'bots'

interface SpaceDetail extends Space {
  about_markdown: string | null
  cover_url: string | null
  cover_hash: string | null
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
  const [isMember, setIsMember] = useState(false)

  const reload = async () => {
    try {
      const [detail, members] = await Promise.all([
        api.get(`/api/spaces/${spaceId}`) as Promise<SpaceDetail>,
        api.get(`/api/spaces/${spaceId}/members`) as Promise<
          Array<{ user_id: string; role: string }>
        >,
      ])
      setSpace(detail)
      const mine = members.find(
        m => m.user_id === currentUser.value?.user_id,
      )
      setCanAdmin(mine?.role === 'owner' || mine?.role === 'admin')
      setIsMember(Boolean(mine))
    } catch {
      setSpace(null)
      setCanAdmin(false)
      setIsMember(false)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void reload() }, [spaceId])

  if (loading) return <Spinner />

  if (!space) {
    return (
      <div class="sh-empty-state">
        <h3>Space not found</h3>
        <Button onClick={() => route(`/spaces/${spaceId}`)}>Back</Button>
      </div>
    )
  }

  // Any space member can reach the "Bots" tab to manage their OWN
  // personal bots. Admin-only guards for the shared space-scope bots
  // live inside SpaceBotsTab. Non-members still get the 403 screen.
  if (!isMember) {
    return (
      <div class="sh-empty-state">
        <div style={{ fontSize: '2rem' }}>🔒</div>
        <h3>Space members only</h3>
        <p class="sh-muted">
          Only members of this space can open its settings.
        </p>
        <Button onClick={() => route(`/spaces/${spaceId}`)}>
          Back to space
        </Button>
      </div>
    )
  }

  // Tabs the active user can see. Non-admins get only the Bots tab
  // (where they manage their own personal automations).
  const visibleTabs: SettingsTab[] = canAdmin
    ? ['general', 'about', 'theme', 'bots']
    : ['bots']
  if (!visibleTabs.includes(activeTab.value)) {
    activeTab.value = visibleTabs[0]
  }

  const tabLabel = (t: SettingsTab): string => {
    switch (t) {
      case 'general': return 'General'
      case 'about':   return 'About'
      case 'theme':   return 'Theme'
      case 'bots':    return 'Bots & automations'
    }
  }

  return (
    <div class="sh-space-settings-page">
      <div class="sh-page-header">
        <h1>⚙ {space.name} — settings</h1>
        <Button variant="secondary"
                onClick={() => route(`/spaces/${spaceId}`)}>
          ← Back to space
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

      {activeTab.value === 'general' && (
        <SpaceSettings space={space} onUpdate={() => void reload()} />
      )}
      {activeTab.value === 'about' && (
        <AboutTab space={space} onSaved={() => void reload()} />
      )}
      {activeTab.value === 'theme' && (
        <SpaceThemeStudio spaceId={space.id} />
      )}
      {activeTab.value === 'bots' && (
        <SpaceBotsTab
          spaceId={space.id}
          canAdmin={canAdmin}
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
  const fileRef = useRef<HTMLInputElement | null>(null)

  const saveAbout = async () => {
    setSaving(true)
    try {
      await api.patch(`/api/spaces/${space.id}`, {
        about_markdown: markdown,
      })
      showToast('About updated', 'success')
      onSaved()
    } catch (err: unknown) {
      showToast(
        `Save failed: ${(err as Error).message ?? err}`, 'error',
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
      showToast('Cover updated', 'success')
    } catch (err: unknown) {
      showToast(
        `Upload failed: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setUploadingCover(false)
      input.value = ''
    }
  }

  const clearCover = async () => {
    if (!confirm('Remove the space cover image?')) return
    try {
      await api.delete(`/api/spaces/${space.id}/cover`)
      setCoverUrl(null)
      onSaved()
      showToast('Cover removed', 'info')
    } catch (err: unknown) {
      showToast(
        `Clear failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  return (
    <div class="sh-form sh-about-editor">
      <section>
        <h3 style={{ margin: 0 }}>Cover image</h3>
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
          Shown as the hero banner at the top of the space. Up to 10 MB;
          large photos are auto-resized and converted to WebP.
        </p>
        <div class="sh-about-cover-preview"
             style={coverUrl ? { backgroundImage: `url(${coverUrl})` } : {}}>
          {!coverUrl && (
            <span class="sh-muted">No cover set yet.</span>
          )}
        </div>
        <div class="sh-row" style={{ gap: 'var(--sh-space-xs)', flexWrap: 'wrap' }}>
          <label class="sh-btn sh-btn--secondary">
            {coverUrl ? 'Change cover' : 'Upload cover'}
            <input ref={fileRef} type="file" accept="image/*"
                   hidden onChange={uploadCover} />
          </label>
          {uploadingCover && <span class="sh-muted">Uploading…</span>}
          {coverUrl && (
            <Button variant="secondary" onClick={clearCover}>
              Remove
            </Button>
          )}
        </div>
      </section>

      <section>
        <h3 style={{ margin: 0 }}>About (Markdown)</h3>
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
          Rendered at the top of the space. Bold, italic, lists,
          links, and code are supported.
        </p>
        <div class="sh-about-editor-grid">
          <label class="sh-about-editor-pane">
            <span class="sh-muted">Write</span>
            <textarea class="sh-about-editor-textarea"
                      value={markdown}
                      rows={12} maxLength={8000}
                      placeholder="# Welcome!\n\nWhat's this space about?"
                      onInput={(e) =>
                        setMarkdown((e.target as HTMLTextAreaElement).value)} />
            <span class="sh-char-count">
              {markdown.length} / 8000
            </span>
          </label>
          <div class="sh-about-editor-pane">
            <span class="sh-muted">Preview</span>
            <div class="sh-about-editor-preview">
              {markdown.trim()
                ? <MarkdownView src={markdown} live />
                : <span class="sh-muted">Nothing to preview yet.</span>}
            </div>
          </div>
        </div>
        <div class="sh-form-actions">
          <Button onClick={saveAbout} loading={saving}>Save about</Button>
        </div>
      </section>
    </div>
  )
}
