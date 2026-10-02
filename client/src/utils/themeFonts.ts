/**
 * Theme font ids → CSS font stacks.
 *
 * Household and space themes store a font as one of the ids the schema
 * allows (``font_family IN ('system','serif','rounded','mono')``,
 * migration 0001) — never a CSS value. Anything that paints the theme
 * maps the id here; an unknown id maps to nothing.
 */
export type FontId = 'system' | 'serif' | 'rounded' | 'mono'

export const FONT_STACKS: Record<FontId, string> = {
  system:  'system-ui, -apple-system, BlinkMacSystemFont, sans-serif',
  serif:   'Georgia, "Times New Roman", serif',
  rounded: '"SF Pro Rounded", "Quicksand", system-ui, sans-serif',
  mono:    'ui-monospace, Menlo, Consolas, monospace',
}

/** The stack for a stored font id, or null for an unknown value. */
export function fontStack(id: string | null | undefined): string | null {
  return id != null && Object.hasOwn(FONT_STACKS, id) ? FONT_STACKS[id as FontId] : null
}
