/**
 * Focus landing spots after an action removes what had focus (a deleted
 * lesson's block, a closed menu) — so a keyboard user never falls back
 * to ``<body>``.
 */

/** DOM id of a timetable's grid section (``tabIndex=-1``). */
export function gridId(timetableId: string): string {
  return `sh-tt-grid-${timetableId}`
}

/** Focus the grid once the current render (dialog close) has settled. */
export function focusGrid(timetableId: string): void {
  setTimeout(() => document.getElementById(gridId(timetableId))?.focus(), 0)
}
