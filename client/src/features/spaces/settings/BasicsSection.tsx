/**
 * Settings › Basics — what the space looks like: cover, picture or emoji,
 * name, short description, About, quick links and theme.
 *
 * The text fields and the emoji are a draft (``useBasicsForm``) saved by
 * the page's save bar. Pictures, quick links and the theme save on their
 * own as soon as they're added — the section says so.
 *
 * The picture preview shows the uploaded icon when there is one (that is
 * what the space shows everywhere), the emoji otherwise.
 */
import { useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { confirmDialog } from '@/components/confirm'
import { EmojiField } from '@/components/EmojiField'
import { MarkdownView } from '@/components/MarkdownView'
import { SpaceThemeStudio, type ThemeDraft } from '@/components/SpaceThemeStudio'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { cssUrl } from '@/utils/cssUrl'
import { SpaceLinksTab } from '@/features/spaces/SpaceLinksTab'
import { spaceGlyph } from '@/features/spaces/spaceGlyph'
import type { BasicsForm } from './useBasicsForm'

interface Props {
  space: {
    id: string
    name: string
    emoji?: string | null
    cover_url?: string | null
    icon_url?: string | null
  }
  form: BasicsForm
  /** Hosted on another household: links and theme are host-local. */
  isRemoteSpace: boolean
  onUpdate: () => void
  /** The theme studio's draft, saved with the page's save bar. */
  onThemeDraft?: (draft: ThemeDraft | null) => void
}

type ImageKind = 'cover' | 'icon'
// For ``i18n:check``: t('space.about.cover_updated') t('space.about.icon_updated')
// t('space.about.cover_remove_confirm') t('space.about.icon_remove_confirm')
// t('space.about.cover_removed') t('space.about.icon_removed')

export function BasicsSection({ space, form, isRemoteSpace, onUpdate, onThemeDraft }: Props) {
  const { name, description, emoji, about } = form.fields
  const [busy, setBusy] = useState<ImageKind | null>(null)
  const [preview, setPreview] = useState(false)

  const upload = async (kind: ImageKind, e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    setBusy(kind)
    try {
      const fd = new FormData()
      fd.append('file', file)
      await api.upload(`/api/spaces/${space.id}/${kind}`, fd)
      showToast(t(kind === 'cover' ? 'space.about.cover_updated' : 'space.about.icon_updated'), 'success')
      onUpdate()
    } catch (err: unknown) {
      showToast(t('space.about.upload_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      setBusy(null)
      input.value = ''
    }
  }

  const remove = async (kind: ImageKind) => {
    const ok = await confirmDialog(
      t(kind === 'cover' ? 'space.about.cover_remove_confirm' : 'space.about.icon_remove_confirm'),
      { destructive: true },
    )
    if (!ok) return
    try {
      await api.delete(`/api/spaces/${space.id}/${kind}`)
      showToast(t(kind === 'cover' ? 'space.about.cover_removed' : 'space.about.icon_removed'), 'info')
      onUpdate()
    } catch (err: unknown) {
      showToast(t('space.about.remove_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  return (
    <div class="sh-ss-card sh-ss-basics">
      <div class="sh-ss-basics__cover"
           style={space.cover_url ? { backgroundImage: cssUrl(space.cover_url) } : undefined}
           data-testid="basics-cover">
        {!space.cover_url && <span class="sh-muted">{t('space.about.cover_empty')}</span>}
        <div class="sh-ss-basics__cover-actions">
          <label class="sh-btn sh-btn--secondary">
            {space.cover_url ? t('space.about.cover_change') : t('space.about.cover_upload')}
            <input type="file" accept="image/*" class="sr-only"
                   onChange={(e) => void upload('cover', e)} />
          </label>
          {space.cover_url && (
            <Button variant="secondary" onClick={() => void remove('cover')}>
              {t('space.about.remove')}
            </Button>
          )}
        </div>
      </div>
      {busy === 'cover' && <p class="sh-muted" role="status">{t('space.about.uploading')}</p>}

      <div class="sh-ss-basics__identity">
        <span class="sh-ss-basics__icon" data-testid="basics-icon"
              style={space.icon_url ? { backgroundImage: cssUrl(space.icon_url) } : undefined}
              aria-hidden="true">
          {!space.icon_url && spaceGlyph(emoji.value || null, name.value || space.name)}
        </span>
        <div class="sh-ss-basics__icon-actions">
          <label class="sh-btn sh-btn--secondary">
            {space.icon_url ? t('space.about.icon_change') : t('space.about.icon_upload')}
            <input type="file" accept="image/*" class="sr-only"
                   onChange={(e) => void upload('icon', e)} />
          </label>
          {space.icon_url && (
            <Button variant="ghost" onClick={() => void remove('icon')}>
              {t('space.about.remove')}
            </Button>
          )}
          {busy === 'icon' && <span class="sh-muted" role="status">{t('space.about.uploading')}</span>}
        </div>
      </div>
      <p class="sh-ss-hint">{t('space.settings_page.basics.pictures_hint')}</p>

      <div class="sh-form sh-ss-basics__form">
        <EmojiField value={emoji} openKey="space-settings-emoji"
                    label={t('space.settings_page.basics.emoji')}
                    hint={space.icon_url ? t('space.settings_page.basics.emoji_hint_icon') : undefined} />
        <label>
          {t('space.settings.name')}
          <input value={name.value} required maxLength={120}
                 onInput={(e) => { name.value = (e.target as HTMLInputElement).value }} />
        </label>
        <div class="sh-ss-basics__field">
          <label for="space-settings-description">
            {t('space.settings_page.basics.short_description')}
          </label>
          <input id="space-settings-description" value={description.value} maxLength={300}
                 aria-describedby="space-settings-description-hint"
                 onInput={(e) => { description.value = (e.target as HTMLInputElement).value }} />
          <span id="space-settings-description-hint" class="sh-ss-hint">
            {t('space.settings_page.basics.short_description_hint')}
          </span>
        </div>
        <div class="sh-ss-basics__about">
          <div class="sh-ss-basics__about-head">
            <label for="space-settings-about">{t('space.about.text_title')}</label>
            <button type="button" class="sh-link" aria-pressed={preview}
                    onClick={() => setPreview(!preview)}>
              {preview ? t('space.settings_page.basics.edit') : t('theme.preview')}
            </button>
          </div>
          {preview ? (
            <div class="sh-about-editor-preview">
              {about.value.trim()
                ? <MarkdownView src={about.value} live />
                : <span class="sh-muted">{t('space.about.preview_empty')}</span>}
            </div>
          ) : (
            <textarea id="space-settings-about" class="sh-about-editor-textarea"
                      value={about.value} rows={8} maxLength={8000}
                      aria-describedby="space-settings-about-hint"
                      placeholder={t('space.about.placeholder')}
                      onInput={(e) => { about.value = (e.target as HTMLTextAreaElement).value }} />
          )}
          <span id="space-settings-about-hint" class="sh-ss-hint">
            {t('space.about.text_hint')} · {about.value.length} / 8000
          </span>
        </div>
      </div>

      {!isRemoteSpace && (
        <>
          <SpaceLinksTab spaceId={space.id} />
          <details class="sh-ss-details">
            <summary>{t('theme.space.title')}</summary>
            <SpaceThemeStudio spaceId={space.id} onDraft={onThemeDraft} />
          </details>
        </>
      )}
    </div>
  )
}
