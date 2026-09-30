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

  if (!toggles.value) return <p class="sh-muted">Loading features...</p>

  const toggle = async (key: keyof Toggles) => {
    if (!toggles.value) return
    const val = toggles.value[key]
    if (typeof val !== 'boolean') return
    const updated = { ...toggles.value, [key]: !val }
    toggles.value = updated
    try {
      await api.put('/api/household/preferences', { toggles: { [key]: !val } })
    } catch {
      showToast('Failed to update', 'error')
      void loadToggles()
    }
  }

  // Bazaar is a per-space feature only — no household-level section
  // toggle, no post-type toggle. Listings live inside spaces and the
  // Bazaar tab in the SPA stays visible to everyone for browsing.
  const featureCards: { value: keyof Toggles; icon: string; title: string; subtitle: string }[] = [
    { value: 'feat_feed', icon: '📮', title: 'Feed', subtitle: 'The shared household activity feed' },
    { value: 'feat_pages', icon: '📄', title: 'Pages', subtitle: 'Wiki-style shared pages' },
    { value: 'feat_tasks', icon: '✅', title: 'Tasks', subtitle: 'Shared to-do lists' },
    { value: 'feat_stickies', icon: '📝', title: 'Stickies', subtitle: 'A shared sticky-note board' },
    { value: 'feat_calendar', icon: '🗓', title: 'Calendar', subtitle: 'The shared household calendar' },
    { value: 'feat_timetable', icon: '🏫', title: 'Timetable', subtitle: 'Weekly school-style timetables' },
    { value: 'feat_presence', icon: '👥', title: 'Presence', subtitle: "Show who's home and online" },
    { value: 'feat_gallery', icon: '🖼', title: 'Gallery', subtitle: 'Shared photo galleries' },
  ]
  const postTypeCards: { value: keyof Toggles; icon: string; title: string; subtitle: string }[] = [
    { value: 'allow_text', icon: '🔤', title: 'Text', subtitle: 'Allow text posts in the feed' },
    { value: 'allow_image', icon: '📷', title: 'Image', subtitle: 'Allow image posts' },
    { value: 'allow_video', icon: '🎬', title: 'Video', subtitle: 'Allow video posts' },
    { value: 'allow_file', icon: '📄', title: 'File', subtitle: 'Allow file attachments' },
    { value: 'allow_poll', icon: '📊', title: 'Poll', subtitle: 'Allow polls' },
    { value: 'allow_schedule', icon: '📅', title: 'Schedule', subtitle: 'Allow scheduled-event posts' },
    { value: 'allow_highlight_share', icon: '⭕', title: 'Highlight share', subtitle: 'Allow sharing highlights to the feed' },
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
        legend="Household features"
        options={featureCards.map(toOption)}
        onToggle={(k) => void toggle(k as keyof Toggles)}
      />
      <CheckboxCardGroup
        legend="Feed post types"
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
