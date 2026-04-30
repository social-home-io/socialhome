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

interface Toggles {
  feat_feed: boolean; feat_pages: boolean; feat_tasks: boolean
  feat_stickies: boolean; feat_calendar: boolean
  allow_text: boolean; allow_image: boolean; allow_video: boolean
  allow_file: boolean; allow_poll: boolean; allow_schedule: boolean
  household_name: string
}

export const toggles = signal<Toggles | null>(null)

export async function loadToggles(): Promise<void> {
  try {
    toggles.value = await api.get('/api/household/features') as Toggles
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
      await api.put('/api/household/features', { toggles: { [key]: !val } })
    } catch {
      showToast('Failed to update', 'error')
      void loadToggles()
    }
  }

  // Bazaar is a per-space feature only — no household-level section
  // toggle, no post-type toggle. Listings live inside spaces and the
  // Bazaar tab in the SPA stays visible to everyone for browsing.
  const features: [keyof Toggles, string][] = [
    ['feat_feed', 'Feed'], ['feat_pages', 'Pages'], ['feat_tasks', 'Tasks'],
    ['feat_stickies', 'Stickies'], ['feat_calendar', 'Calendar'],
  ]
  const postTypes: [keyof Toggles, string][] = [
    ['allow_text', 'Text'], ['allow_image', 'Image'], ['allow_video', 'Video'],
    ['allow_file', 'File'], ['allow_poll', 'Poll'],
    ['allow_schedule', 'Schedule'],
  ]

  return (
    <div class="sh-toggles">
      <h3>Sections</h3>
      {features.map(([key, label]) => (
        <label key={key} class="sh-toggle-row">
          <input type="checkbox" checked={!!toggles.value![key]}
            onChange={() => toggle(key)} />
          {label}
        </label>
      ))}
      <h3>Post types</h3>
      {postTypes.map(([key, label]) => (
        <label key={key} class="sh-toggle-row">
          <input type="checkbox" checked={!!toggles.value![key]}
            onChange={() => toggle(key)} />
          {label}
        </label>
      ))}
    </div>
  )
}
