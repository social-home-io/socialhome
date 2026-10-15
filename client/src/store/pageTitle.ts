/**
 * Page title — shared signal driving the heading shown in the TopBar.
 *
 * Pages call :func:`useTitle` from their top-level component; the
 * TopBar renders ``pageTitle.value`` so every page shows its label
 * inline with the search bar (saves one row of vertical space vs. an
 * h1 inside the page body).
 *
 * Static pages (Calendar, Shopping, Settings, …) pass a literal
 * string. Dynamic pages (the active task list's name, a space's
 * name) re-call :func:`useTitle` whenever the underlying value
 * changes — the hook updates the signal on every render so reactive
 * computations stay live.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import type { PersonHint } from '@/utils/personHint'

export const pageTitle = signal<string>('')

/** Optional avatar / name pair rendered next to the page title. Pages
 *  that surface a person-focused view (a DM thread, a profile, a host-
 *  approval queue) set this so the TopBar shows the peer's face right
 *  next to their name — same idiom as the row in the inbox list, so
 *  the user's eye doesn't have to re-anchor when they tap into a
 *  thread. ``null`` collapses the avatar and the TopBar renders the
 *  title alone (the default for most pages). */
export interface PageAvatar {
  /** Backend-signed src — comes pre-tagged so the browser can load it
   *  via raw ``<img>`` without an Authorization header. */
  src: string | null
  /** Used for the initials fallback when ``src`` is null / errors. */
  name: string
  /** The person this heading is about (a 1:1 DM's peer). Set → the
   *  TopBar avatar + title open their person sheet. */
  personId?: string
  /** What the page knows about them, for an instant sheet. */
  personHint?: PersonHint
}
export const pageTitleAvatar = signal<PageAvatar | null>(null)

/** Suffix appended to ``document.title`` so a glance at the browser
 *  tab still says "Social Home" — the per-page label tells the user
 *  *which* page they're on. */
const DOC_TITLE_BRAND = 'Social Home'

function setDocTitle(label: string): void {
  if (typeof document === 'undefined') return
  document.title = label ? `${label} · ${DOC_TITLE_BRAND}` : DOC_TITLE_BRAND
}

/** Set the TopBar title for the current page, and the browser tab
 *  title to match. Without the document.title sync the tab always
 *  read "Social Home" no matter which page was open — multiple tabs
 *  were indistinguishable.
 *
 *  Calling without an argument (or with an empty string) clears the
 *  title — useful for pages that intentionally render their own
 *  header (e.g. SpaceFeedPage with its hero cover).
 *
 *  Cleans up on unmount so navigating away resets the signal even
 *  when the next route hasn't mounted yet.
 */
export function useTitle(title: string): void {
  useEffect(() => {
    pageTitle.value = title
    setDocTitle(title)
    return () => {
      // Reset to empty so the topbar doesn't show a stale title from
      // the page we just left during the brief gap before the new
      // page's ``useTitle`` runs.
      pageTitle.value = ''
      setDocTitle('')
    }
  }, [title])
}

/** Set an avatar next to the page title (and clear it on unmount).
 *  Pair with :func:`useTitle` for the name. */
export function useTitleAvatar(avatar: PageAvatar | null): void {
  const src = avatar?.src ?? null
  const name = avatar?.name ?? ''
  const personId = avatar?.personId ?? null
  useEffect(() => {
    pageTitleAvatar.value = avatar
    return () => { pageTitleAvatar.value = null }
  }, [src, name, personId])
}
