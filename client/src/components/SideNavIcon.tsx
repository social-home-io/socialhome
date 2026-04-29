/**
 * SideNavIcon — single source for the sidebar icon set.
 *
 * Real SVG glyphs (24×24, stroke-2, currentColor) rather than emoji
 * because emoji render inconsistently across OSes and sizes. The icon
 * key matches the sidebar item's `key`; the name → glyph map lives
 * here so the SideNav data array stays pure.
 */
import type { JSX } from 'preact'

export type SideNavIconName =
  | 'feed' | 'calendar' | 'tasks' | 'shopping' | 'presence'
  | 'gallery' | 'pages' | 'stickies'
  | 'messages' | 'calls'
  | 'spaces' | 'bazaar' | 'corner'
  | 'parent-control' | 'settings' | 'connections' | 'admin'

interface Props {
  name: SideNavIconName
}

const COMMON: JSX.SVGAttributes<SVGSVGElement> = {
  width: 20,
  height: 20,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  'stroke-width': 2,
  'stroke-linecap': 'round',
  'stroke-linejoin': 'round',
  'aria-hidden': 'true',
  focusable: 'false',
}

export function SideNavIcon({ name }: Props): JSX.Element {
  return (
    <span class="sh-sidenav-icon" data-icon={name}>
      {GLYPHS[name]}
    </span>
  )
}

const GLYPHS: Record<SideNavIconName, JSX.Element> = {
  feed: (
    <svg {...COMMON}>
      <line x1="3" y1="6" x2="21" y2="6" />
      <line x1="3" y1="12" x2="21" y2="12" />
      <line x1="3" y1="18" x2="14" y2="18" />
    </svg>
  ),
  calendar: (
    <svg {...COMMON}>
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <line x1="3" y1="10" x2="21" y2="10" />
      <line x1="8" y1="3" x2="8" y2="7" />
      <line x1="16" y1="3" x2="16" y2="7" />
    </svg>
  ),
  tasks: (
    <svg {...COMMON}>
      <polyline points="4 12 8 16 20 6" />
      <polyline points="4 6 6 8" opacity="0.55" />
      <polyline points="4 18 6 20" opacity="0.55" />
    </svg>
  ),
  shopping: (
    <svg {...COMMON}>
      <path d="M3 5h2l2.4 11.2a2 2 0 0 0 2 1.6h7.4a2 2 0 0 0 2-1.5L21 8H6" />
      <circle cx="9" cy="20" r="1.4" />
      <circle cx="18" cy="20" r="1.4" />
    </svg>
  ),
  presence: (
    <svg {...COMMON}>
      <circle cx="9" cy="9" r="3.2" />
      <path d="M3 20a6 6 0 0 1 12 0" />
      <circle cx="17" cy="7" r="2.4" />
      <path d="M14.5 13.5A4.6 4.6 0 0 1 21 19" />
    </svg>
  ),
  gallery: (
    <svg {...COMMON}>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <circle cx="9" cy="10.5" r="1.6" />
      <polyline points="3 17 9 12 14 17 17 14 21 18" />
    </svg>
  ),
  pages: (
    <svg {...COMMON}>
      <path d="M7 3h8l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" />
      <polyline points="14 3 14 8 19 8" />
      <line x1="9" y1="13" x2="16" y2="13" />
      <line x1="9" y1="17" x2="14" y2="17" />
    </svg>
  ),
  stickies: (
    <svg {...COMMON}>
      <path d="M5 4h10l4 4v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z" />
      <polyline points="14 4 14 9 19 9" />
    </svg>
  ),
  messages: (
    <svg {...COMMON}>
      <path d="M4 5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9l-5 4V6a1 1 0 0 1 1-1z" />
    </svg>
  ),
  calls: (
    <svg {...COMMON}>
      <path d="M5 4h3l2 5-2.5 1.5a12 12 0 0 0 6 6L15 14l5 2v3a1 1 0 0 1-1 1A15 15 0 0 1 4 5a1 1 0 0 1 1-1z" />
    </svg>
  ),
  spaces: (
    <svg {...COMMON}>
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </svg>
  ),
  bazaar: (
    <svg {...COMMON}>
      <path d="M11 3 3 11l8 8 8-8-2-7z" />
      <circle cx="14.5" cy="9.5" r="1.2" />
    </svg>
  ),
  corner: (
    <svg {...COMMON}>
      <circle cx="12" cy="12" r="9" />
      <polygon points="14 14 9 16 11 11 16 9" />
    </svg>
  ),
  'parent-control': (
    <svg {...COMMON}>
      <path d="M12 3 4 6v6a9 9 0 0 0 8 9 9 9 0 0 0 8-9V6z" />
      <polyline points="9 12 11 14 15 10" />
    </svg>
  ),
  settings: (
    <svg {...COMMON}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  ),
  connections: (
    <svg {...COMMON}>
      <path d="M10 14a4 4 0 0 1 0-5.6l3-3a4 4 0 0 1 5.6 5.6l-1.5 1.5" />
      <path d="M14 10a4 4 0 0 1 0 5.6l-3 3a4 4 0 0 1-5.6-5.6l1.5-1.5" />
    </svg>
  ),
  admin: (
    <svg {...COMMON}>
      <path d="M14.7 3.3 16 4.6l3.4-3.4 3 3-3.4 3.4 1.3 1.3-2 2-1.3-1.3-7 7L8 18l-2 1-1 2-2-3 2-1 1-2 2-2 7-7-1.3-1.3z" />
    </svg>
  ),
}
