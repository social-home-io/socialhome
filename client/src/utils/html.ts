/**
 * HTML-string helpers for the few surfaces that must hand markup to a
 * non-Preact renderer — Leaflet popups, tooltips and ``divIcon`` take
 * strings and mount them via ``innerHTML``.
 *
 * Anything interpolated into such a string (a zone name, a member's
 * label, a peer household's display name) may come from another
 * household over federation, so it goes through :func:`escapeHtml`
 * — never raw.
 */

const _ENTITIES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

/** Escape the five HTML-significant characters so ``s`` renders as
 *  literal text in element content and in quoted attribute values. */
export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => _ENTITIES[c]!)
}
