/**
 * icons — the line icons Highlights draws (Today rows, the viewer).
 * 24-px grid, ``currentColor`` strokes, hidden from screen readers: the
 * control around an icon carries the words.
 */
import type { AudienceIcon } from './today'

export const SVG = {
  viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2,
  'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false',
} as const

export const CHIP_ICON: Record<AudienceIcon, preact.JSX.Element> = {
  households: (
    <svg {...SVG} width={14} height={14}>
      <circle cx="9" cy="8" r="3" /><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6" />
      <circle cx="17" cy="9" r="2.5" /><path d="M17 14c2.5 0 4 1.8 4 4.5" />
    </svg>
  ),
  home: (
    <svg {...SVG} width={14} height={14}><path d="M4 11l8-7 8 7v9H4z" /><path d="M10 20v-5h4v5" /></svg>
  ),
  lock: (
    <svg {...SVG} width={14} height={14}><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></svg>
  ),
  people: (
    <svg {...SVG} width={14} height={14}>
      <circle cx="8" cy="8" r="3" /><circle cx="16" cy="8" r="3" />
      <path d="M2 20c0-3 2.7-5 6-5M22 20c0-3-2.7-5-6-5M9 20c0-2.5 1.3-4 3-4s3 1.5 3 4" />
    </svg>
  ),
}

/** The viewer's controls. Chevrons carry ``sh-flip-rtl``: "next" points
 *  to the end of the line in either direction. */
export const VIEWER_ICON = {
  pause: (<svg {...SVG} width={22} height={22}><path d="M8 5v14M16 5v14" stroke-width={3} /></svg>),
  play: (<svg {...SVG} width={22} height={22}><path d="M7 4l13 8-13 8z" fill="currentColor" /></svg>),
  more: (
    <svg {...SVG} width={22} height={22}>
      <circle cx="5" cy="12" r="1.6" fill="currentColor" /><circle cx="12" cy="12" r="1.6" fill="currentColor" />
      <circle cx="19" cy="12" r="1.6" fill="currentColor" />
    </svg>
  ),
  close: (<svg {...SVG} width={22} height={22}><path d="M6 6l12 12M18 6L6 18" /></svg>),
  prev: (<svg {...SVG} width={22} height={22} class="sh-flip-rtl"><path d="M15 6l-6 6 6 6" /></svg>),
  next: (<svg {...SVG} width={22} height={22} class="sh-flip-rtl"><path d="M9 6l6 6-6 6" /></svg>),
  eye: (<svg {...SVG} width={18} height={18}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></svg>),
  share: (<svg {...SVG} width={18} height={18}><path d="M12 15V3M7 8l5-5 5 5" /><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6" /></svg>),
  link: (
    <svg {...SVG} width={20} height={20}>
      <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" />
      <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />
    </svg>
  ),
  trash: (<svg {...SVG} width={20} height={20}><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" /></svg>),
  person: (<svg {...SVG} width={20} height={20}><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7" /></svg>),
  flag: (<svg {...SVG} width={20} height={20}><path d="M5 21V4M5 4h11l-2 4 2 4H5" /></svg>),
  feed: (<svg {...SVG} width={20} height={20}><path d="M4 11l8-7 8 7v9H4z" /><path d="M10 20v-5h4v5" /></svg>),
  space: (<svg {...SVG} width={20} height={20}><circle cx="9" cy="8" r="3" /><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6" /><circle cx="17" cy="9" r="2.5" /><path d="M17 14c2.5 0 4 1.8 4 4.5" /></svg>),
  send: (<svg {...SVG} width={20} height={20} class="sh-flip-rtl"><path d="M4 12l16-8-6 16-2-6z" /></svg>),
  sound: (<svg {...SVG} width={20} height={20}><path d="M4 9v6h4l5 4V5L8 9z" /><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11" /></svg>),
  muted: (<svg {...SVG} width={20} height={20}><path d="M4 9v6h4l5 4V5L8 9z" /><path d="M17 9l5 6M22 9l-5 6" /></svg>),
}
