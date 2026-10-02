/**
 * Space post-layout ids.
 *
 * A space theme stores one of the ids the schema allows
 * (``post_layout IN ('card','compact','magazine')``, migration 0001).
 * :func:`useSpaceTheme` paints it as ``data-post-layout`` on ``<html>``
 * and ``app.css`` styles the space feed from that attribute; the theme
 * studio's preview carries the same attribute so it shows the result.
 * ``card`` is the column default and the feed's own look.
 */
export type LayoutId = 'card' | 'compact' | 'magazine'

/** Every id the server accepts, in picker order. */
export const LAYOUT_IDS: readonly LayoutId[] = ['card', 'compact', 'magazine']

export const DEFAULT_LAYOUT: LayoutId = 'card'

export function isLayoutId(id: unknown): id is LayoutId {
  return typeof id === 'string' && (LAYOUT_IDS as readonly string[]).includes(id)
}
