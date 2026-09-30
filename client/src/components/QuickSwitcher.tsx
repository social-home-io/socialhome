/**
 * QuickSwitcher — Cmd+K navigation (§23.69).
 */
import { signal } from '@preact/signals'

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
    { label: 'Feed', href: '/', type: 'page' },
    { label: 'Spaces', href: '/spaces', type: 'page' },
    { label: 'Messages', href: '/dms', type: 'page' },
    { label: 'Calendar', href: '/calendar', type: 'page' },
    { label: 'Timetable', href: '/calendar?tab=timetable', type: 'page' },
    { label: 'Tasks', href: '/organize', type: 'page' },
    { label: 'Shopping', href: '/organize?tab=shopping', type: 'page' },
    { label: 'Stickies', href: '/organize?tab=stickies', type: 'page' },
    { label: 'Pages', href: '/pages', type: 'page' },
    { label: 'Notifications', href: '/notifications', type: 'page' },
    { label: 'Settings', href: '/settings', type: 'page' },
    { label: 'Admin', href: '/admin', type: 'page' },
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
        <input class="sh-switcher-input" placeholder="Go to..."
          value={query.value} autofocus
          onInput={(e) => query.value = (e.target as HTMLInputElement).value} />
        <div class="sh-switcher-results">
          {filtered.map(item => (
            <a key={item.href} class="sh-switcher-item" href={item.href}
              onClick={() => open.value = false}>
              {item.label}
            </a>
          ))}
        </div>
      </div>
    </div>
  )
}
