/**
 * Theme font ids → CSS font stacks.
 *
 * Household and space themes store a font as one of the ids the schema
 * allows (``font_family IN ('system','serif','rounded','mono')``,
 * migration 0001) — never a CSS value. Anything that paints the theme
 * maps the id here; an unknown id maps to nothing.
 *
 * ``system`` (the column default) is "no override": the app's own body
 * font (``--sh-font-family``, tokens.css). At household scope that is
 * the app font; inside a space it means "same as the household".
 *
 * ``rounded`` leads with Nunito, bundled with the SPA
 * (``assets/fonts/nunito``, SIL OFL 1.1, latin subset) so it is rounded
 * on every device — never fetched from a third-party CDN.
 */
export type FontId = 'system' | 'serif' | 'rounded' | 'mono'

/** Every id the server accepts, in picker order. */
export const FONT_IDS: readonly FontId[] = ['system', 'serif', 'rounded', 'mono']

/** True for a font id the server accepts (anything else — a legacy CSS
 *  stack, a typo — is not one). */
export function isFontId(id: unknown): id is FontId {
  return typeof id === 'string' && (FONT_IDS as readonly string[]).includes(id)
}

/** The app's own body font — what ``system`` (no override) resolves to. */
export const APP_FONT_STACK = 'var(--sh-font-family)'

export const FONT_STACKS: Record<FontId, string> = {
  system:  APP_FONT_STACK,
  serif:   'Georgia, "Times New Roman", serif',
  rounded: '"Nunito Variable", "SF Pro Rounded", "Quicksand", system-ui, sans-serif',
  mono:    'ui-monospace, Menlo, Consolas, monospace',
}

/** The stack for a stored font id, or null for an unknown value. */
export function fontStack(id: string | null | undefined): string | null {
  return id != null && Object.hasOwn(FONT_STACKS, id) ? FONT_STACKS[id as FontId] : null
}

/** The stack a theme *overrides* the font with — null for ``system``
 *  (no override) and for anything unknown. */
export function fontOverride(id: string | null | undefined): string | null {
  return id === 'system' ? null : fontStack(id)
}
