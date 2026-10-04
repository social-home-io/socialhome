/**
 * QuickSwitcher — Cmd+K navigation (§23.69).
 */
import { signal } from '@preact/signals'
import { addBase } from '@/baseUrl'
import { t } from '@/i18n/i18n'

const open = signal(false)
const query = signal('')
const items = signal<{ label: string; href: string; type: string }[]>([])

// Listen for Cmd+K / Ctrl+K
if (typeof window !== 'undefined') {
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault()
      open.value = !open.value
      query.value = ''
      items.value = defaultItems()
    }
    if (e.key === 'Escape') open.value = false
  })
}

function defaultItems() {
  return [
    { label: t('nav.feed'), href: '/', type: 'page' },
    { label: t('nav.spaces'), href: '/spaces', type: 'page' },
    { label: t('nav.messages'), href: '/dms', type: 'page' },
    { label: t('nav.calendar'), href: '/calendar', type: 'page' },
    { label: t('nav.timetable'), href: '/calendar?tab=timetable', type: 'page' },
    { label: t('nav.tasks'), href: '/organize', type: 'page' },
    { label: t('nav.shopping'), href: '/organize?tab=shopping', type: 'page' },
    { label: t('nav.stickies'), href: '/organize?tab=stickies', type: 'page' },
    { label: t('nav.pages'), href: '/pages', type: 'page' },
    { label: t('nav.notifications'), href: '/notifications', type: 'page' },
    { label: t('nav.settings'), href: '/settings', type: 'page' },
    { label: t('nav.admin'), href: '/admin', type: 'page' },
  ]
}

export function QuickSwitcher() {
  if (!open.value) return null

  const filtered = query.value
    ? items.value.filter(i => i.label.toLowerCase().includes(query.value.toLowerCase()))
    : items.value

  return (
    <div class="sh-switcher-overlay" onClick={() => open.value = false}>
      <div class="sh-switcher" onClick={(e) => e.stopPropagation()}>
        <input class="sh-switcher-input" placeholder={t('nav.go_to')}
          value={query.value} autofocus
          onInput={(e) => query.value = (e.target as HTMLInputElement).value} />
        <div class="sh-switcher-results">
          {filtered.map(item => (
            <a key={item.href} class="sh-switcher-item" href={addBase(item.href)}
              onClick={() => open.value = false}>
              {item.label}
            </a>
          ))}
        </div>
      </div>
    </div>
  )
}
