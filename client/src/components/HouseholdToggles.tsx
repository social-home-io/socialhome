/**
 * HouseholdToggles — feature toggle grid in admin (§23.13).
 *
 * Listens for ``household.config_changed`` WS events so a toggle flip
 * on another device refreshes this one live (spec §18).
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { showToast } from './Toast'
import { CheckboxCardGroup, type CheckboxCardOption } from './CheckboxCardGroup'
import { t } from '@/i18n/i18n'

interface Toggles {
  feat_feed: boolean; feat_pages: boolean; feat_tasks: boolean
  feat_stickies: boolean; feat_calendar: boolean; feat_presence: boolean
  feat_gallery: boolean; feat_timetable: boolean
  allow_text: boolean; allow_image: boolean; allow_video: boolean
  allow_file: boolean; allow_poll: boolean; allow_schedule: boolean
  allow_highlight_share: boolean
  /** Author-side link preview cards (default on). */
  allow_link_preview: boolean
  household_name: string
}

export const toggles = signal<Toggles | null>(null)

export async function loadToggles(): Promise<void> {
  try {
    toggles.value = await api.get('/api/household/preferences') as Toggles
  } catch {
    /* auth failure or offline — leave prior state */
  }
}

export function HouseholdToggles() {
  useEffect(() => {
    void loadToggles()
    const off = ws.on('household.config_changed', () => { void loadToggles() })
    return () => { off() }
  }, [])

  if (!toggles.value) return <p class="sh-muted">{t('common.loading')}</p>

  const toggle = async (key: keyof Toggles) => {
    if (!toggles.value) return
    const val = toggles.value[key]
    if (typeof val !== 'boolean') return
    const updated = { ...toggles.value, [key]: !val }
    toggles.value = updated
    try {
      await api.put('/api/household/preferences', { toggles: { [key]: !val } })
    } catch {
      showToast(t('household.toggles.update_failed'), 'error')
      void loadToggles()
    }
  }

  // Bazaar is a per-space feature only — no household-level section
  // toggle, no post-type toggle. Listings live inside spaces and the
  // Bazaar tab in the SPA stays visible to everyone for browsing.
  const featureCards: { value: keyof Toggles; icon: string; title: string; subtitle: string }[] = [
    { value: 'feat_feed', icon: '📮', title: t('nav.feed'), subtitle: t('household.toggles.feed_hint') },
    { value: 'feat_pages', icon: '📄', title: t('nav.pages'), subtitle: t('household.toggles.pages_hint') },
    { value: 'feat_tasks', icon: '✅', title: t('nav.tasks'), subtitle: t('household.toggles.tasks_hint') },
    { value: 'feat_stickies', icon: '📝', title: t('nav.stickies'), subtitle: t('household.toggles.stickies_hint') },
    { value: 'feat_calendar', icon: '🗓', title: t('nav.calendar'), subtitle: t('household.toggles.calendar_hint') },
    { value: 'feat_timetable', icon: '🏫', title: t('nav.timetable'), subtitle: t('household.toggles.timetable_hint') },
    { value: 'feat_presence', icon: '👥', title: t('nav.presence'), subtitle: t('household.toggles.presence_hint') },
    { value: 'feat_gallery', icon: '🖼', title: t('nav.gallery'), subtitle: t('household.toggles.gallery_hint') },
  ]
  // Titles reuse the space settings' post-type names.
  const postTypeCards: { value: keyof Toggles; icon: string; title: string; subtitle: string }[] = [
    { value: 'allow_text', icon: '🔤', title: t('space.post_type.text'), subtitle: t('household.toggles.post_text_hint') },
    { value: 'allow_image', icon: '📷', title: t('space.post_type.image'), subtitle: t('household.toggles.post_image_hint') },
    { value: 'allow_video', icon: '🎬', title: t('space.post_type.video'), subtitle: t('household.toggles.post_video_hint') },
    { value: 'allow_file', icon: '📄', title: t('space.post_type.file'), subtitle: t('household.toggles.post_file_hint') },
    { value: 'allow_poll', icon: '📊', title: t('space.post_type.poll'), subtitle: t('household.toggles.post_poll_hint') },
    { value: 'allow_schedule', icon: '📅', title: t('space.post_type.schedule'), subtitle: t('household.toggles.post_schedule_hint') },
    { value: 'allow_highlight_share', icon: '⭕', title: t('space.post_type.highlight_share'), subtitle: t('household.toggles.post_highlight_share_hint') },
  ]

  const linkCards: { value: keyof Toggles; icon: string; title: string; subtitle: string }[] = [
    {
      value: 'allow_link_preview',
      icon: '🔗',
      title: t('link_preview.admin_title'),
      subtitle: t('link_preview.admin_subtitle'),
    },
  ]

  const toOption = (c: { value: keyof Toggles; icon: string; title: string; subtitle: string }): CheckboxCardOption => ({
    value: c.value,
    icon: c.icon,
    title: c.title,
    subtitle: c.subtitle,
    checked: !!toggles.value![c.value],
  })

  return (
    <div class="sh-toggles">
      <CheckboxCardGroup
        legend={t('household.toggles.features_legend')}
        options={featureCards.map(toOption)}
        onToggle={(k) => void toggle(k as keyof Toggles)}
      />
      <CheckboxCardGroup
        legend={t('household.toggles.post_types_legend')}
        options={postTypeCards.map(toOption)}
        onToggle={(k) => void toggle(k as keyof Toggles)}
      />
      <CheckboxCardGroup
        legend={t('link_preview.admin_legend')}
        options={linkCards.map(toOption)}
        onToggle={(k) => void toggle(k as keyof Toggles)}
      />
    </div>
  )
}
