/**
 * ConnIcon — the Connections page's one outline icon set.
 *
 * Same attributes as ``SideNavIcon`` (24×24 viewBox, stroke 2, round caps
 * and joins, currentColor). Size comes from CSS (``.sh-conn-ico``: 16 px
 * inline, 20 px in rows, 11 px inside a status badge) so a glyph never
 * carries its own width. Always decorative — the surrounding text or
 * ``aria-label`` names the control.
 */
import type { JSX } from 'preact'

export type ConnIconName =
  | 'bolt' | 'globe' | 'cloud' | 'chev' | 'more' | 'check' | 'up' | 'bang'
  | 'dots' | 'off' | 'list' | 'map' | 'plus' | 'addr' | 'relay' | 'download'
  | 'info' | 'alert' | 'copy' | 'users' | 'qr' | 'spaces' | 'pencil' | 'clock' | 'x'
  | 'message' | 'phone' | 'home' | 'tag'

const PATHS: Record<ConnIconName, JSX.Element> = {
  bolt: <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />,
  globe: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
    </>
  ),
  cloud: <path d="M19 18H6a4 4 0 0 1 0-8 5 5 0 0 1 9.6-2A4 4 0 0 1 19 18z" />,
  chev: <path d="m9 6 6 6-6 6" />,
  more: (
    <>
      <circle cx="5" cy="12" r="1.3" />
      <circle cx="12" cy="12" r="1.3" />
      <circle cx="19" cy="12" r="1.3" />
    </>
  ),
  check: <path d="m5 12 5 5 9-10" />,
  up: <path d="M12 19V6M6 11l6-6 6 6" />,
  bang: <path d="M12 6v8M12 18h.01" />,
  dots: <path d="M6 12h.01M12 12h.01M18 12h.01" />,
  off: <path d="M2 2l20 20M8.5 16.5a5 5 0 0 1 7 0M5 13a10 10 0 0 1 5.17-2.8M19 13a10 10 0 0 0-2.2-1.6M12 20h.01" />,
  list: <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />,
  map: <path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14" />,
  plus: <path d="M12 5v14M5 12h14" />,
  addr: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18" />
      <path d="M12 3c3 3.5 3 14.5 0 18" />
    </>
  ),
  relay: <path d="M4 12h4l3-6 3 12 3-6h3" />,
  download: <path d="M12 4v11M7 10l5 5 5-5M5 20h14" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v6M12 7.5h.01" />
    </>
  ),
  alert: (
    <>
      <path d="M12 3 2 20h20L12 3z" />
      <path d="M12 10v4M12 17h.01" />
    </>
  ),
  copy: (
    <>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h8" />
    </>
  ),
  users: (
    <>
      <circle cx="9" cy="9" r="3.2" />
      <path d="M3 20a6 6 0 0 1 12 0" />
      <circle cx="17" cy="7" r="2.4" />
      <path d="M14.5 13.5A4.6 4.6 0 0 1 21 19" />
    </>
  ),
  qr: (
    <>
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <path d="M14 14h3v3M21 14v.01M14 21h3M21 17v4" />
    </>
  ),
  pencil: <path d="M4 20h4L19 9l-4-4L4 16v4zM14 6l4 4" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  x: <path d="M6 6l12 12M18 6 6 18" />,
  message: <path d="M4 5h16v11H8l-4 4z" />,
  phone: <path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z" />,
  home: (
    <>
      <path d="M3 11 12 4l9 7" />
      <path d="M5 10v10h14V10" />
      <path d="M10 20v-5h4v5" />
    </>
  ),
  tag: (
    <>
      <path d="M3 12V3h9l9 9-9 9z" />
      <circle cx="7.5" cy="7.5" r="1.5" />
    </>
  ),
  spaces: (
    <>
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </>
  ),
}

/** Raw glyph markup for the SVG constellation (same paths, no wrapper). */
export function connGlyph(name: ConnIconName): JSX.Element {
  return PATHS[name]
}

export function ConnIcon({ name }: { name: ConnIconName }) {
  return (
    <svg
      class="sh-conn-ico"
      data-glyph={name}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  )
}
