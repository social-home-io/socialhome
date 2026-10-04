/**
 * SpaceLinksTab — admin editor for the sidebar quick-links.
 *
 * Lists every configured link, lets owners/admins add new ones,
 * edit in place, reorder (via the position field), and delete.
 * Members see the links rendered in the space hero but can't edit
 * them here (the tab is hidden for non-admins by SpaceSettingsPage).
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { t } from '@/i18n/i18n'

interface SpaceLink {
  id: string
  label: string
  url: string
  position: number
}

interface Props {
  spaceId: string
}

export function SpaceLinksTab({ spaceId }: Props) {
  const [links, setLinks] = useState<SpaceLink[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [draft, setDraft] = useState<{ label: string; url: string }>({
    label: '',
    url: '',
  })

  const reload = async () => {
    setLoading(true)
    try {
      const body = await api.get(`/api/spaces/${spaceId}/links`) as {
        links: SpaceLink[]
      }
      setLinks(body.links)
    } catch (err: unknown) {
      showToast(t('space.links.load_failed', { error: (err as Error).message }), 'error')
      setLinks([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void reload() }, [spaceId])

  const createLink = async (e: Event) => {
    e.preventDefault()
    const label = draft.label.trim()
    const url = draft.url.trim()
    if (!label || !url) {
      showToast(t('space.links.required'), 'error')
      return
    }
    setSaving(true)
    try {
      await api.post(`/api/spaces/${spaceId}/links`, {
        label,
        url,
        position: links.length,
      })
      setDraft({ label: '', url: '' })
      await reload()
      showToast(t('space.links.added'), 'success')
    } catch (err: unknown) {
      showToast(t('space.links.add_failed', { error: (err as Error).message }), 'error')
    } finally {
      setSaving(false)
    }
  }

  const updateLink = async (link: SpaceLink, patch: Partial<SpaceLink>) => {
    try {
      await api.patch(
        `/api/spaces/${spaceId}/links/${link.id}`,
        patch,
      )
      await reload()
    } catch (err: unknown) {
      showToast(t('space.links.save_failed', { error: (err as Error).message }), 'error')
    }
  }

  const deleteLink = async (link: SpaceLink) => {
    if (!await confirmDialog(t('space.links.remove_confirm', { label: link.label }), { destructive: true })) return
    try {
      await api.delete(`/api/spaces/${spaceId}/links/${link.id}`)
      await reload()
      showToast(t('space.links.removed'), 'info')
    } catch (err: unknown) {
      showToast(t('space.links.remove_failed', { error: (err as Error).message }), 'error')
    }
  }

  const moveLink = async (index: number, delta: -1 | 1) => {
    const next = index + delta
    if (next < 0 || next >= links.length) return
    const a = links[index]
    const b = links[next]
    // Swap positions so the list order matches user intent.
    await Promise.all([
      api.patch(`/api/spaces/${spaceId}/links/${a.id}`, { position: b.position }),
      api.patch(`/api/spaces/${spaceId}/links/${b.id}`, { position: a.position }),
    ])
    await reload()
  }

  return (
    <section class="sh-space-links-tab">
      <h2>{t('space.links.title')}</h2>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)' }}>
        {t('space.links.intro')}
      </p>

      {loading && <Spinner />}

      {!loading && links.length === 0 && (
        <p class="sh-muted">{t('space.links.empty')}</p>
      )}

      {!loading && links.length > 0 && (
        <ul class="sh-space-links-editor" role="list">
          {links.map((link, i) => (
            <li key={link.id} class="sh-space-links-editor__row">
              <input
                class="sh-space-links-editor__label"
                value={link.label}
                aria-label={t('space.links.label_aria')}
                onBlur={(e) => {
                  const next = (e.target as HTMLInputElement).value.trim()
                  if (next && next !== link.label) {
                    void updateLink(link, { label: next })
                  }
                }}
              />
              <input
                class="sh-space-links-editor__url"
                value={link.url}
                aria-label={t('space.links.url_aria')}
                onBlur={(e) => {
                  const next = (e.target as HTMLInputElement).value.trim()
                  if (next && next !== link.url) {
                    void updateLink(link, { url: next })
                  }
                }}
              />
              <div class="sh-space-links-editor__actions">
                <button type="button"
                        class="sh-icon-btn"
                        aria-label={t('space.links.move_up')}
                        disabled={i === 0}
                        onClick={() => void moveLink(i, -1)}>↑</button>
                <button type="button"
                        class="sh-icon-btn"
                        aria-label={t('space.links.move_down')}
                        disabled={i === links.length - 1}
                        onClick={() => void moveLink(i, 1)}>↓</button>
                <button type="button"
                        class="sh-icon-btn sh-icon-btn--danger"
                        aria-label={t('space.links.remove_aria', { label: link.label })}
                        onClick={() => void deleteLink(link)}>✕</button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <form class="sh-space-links-editor__create" onSubmit={createLink}>
        <h3>{t('space.links.add')}</h3>
        <input type="text"
               value={draft.label}
               placeholder={t('space.links.label_placeholder')}
               maxLength={64}
               onInput={(e) =>
                 setDraft({ ...draft, label: (e.target as HTMLInputElement).value })
               } />
        <input type="url"
               value={draft.url}
               placeholder="https://…"
               maxLength={2048}
               onInput={(e) =>
                 setDraft({ ...draft, url: (e.target as HTMLInputElement).value })
               } />
        <Button type="submit" loading={saving}>{t('space.links.add')}</Button>
      </form>
    </section>
  )
}
