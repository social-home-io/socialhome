/**
 * PageConflictView — pick between versions of a page that were edited at
 * the same time (§4.4.4.1).
 *
 * Two uses, one layout:
 *   - **Stale save** (household or space): someone saved while this editor
 *     was open — two panels, "Your version" / "Their version".
 *   - **Federated conflict** (space pages, v_48): concurrent edits from
 *     other households could not be merged automatically — one panel per
 *     version (up to three), labelled by author and when, plus the
 *     viewer's unsaved draft when a save ran into the conflict.
 *
 * Each panel keeps its version in one click; "Merge by hand" puts every
 * version into the editor with conflict markers. The parent owns what a
 * choice does (a PATCH, or a ``resolve-conflict`` POST).
 */
import type { ComponentChildren } from 'preact'
import { Button } from '@/components/Button'
import { MarkdownView } from '@/components/MarkdownView'
import { t } from '@/i18n/i18n'

export interface ConflictPanel {
  /** Stable key (a version hash, ``draft``, ``mine`` / ``theirs``). */
  key: string
  /** Panel heading (who wrote it). */
  heading: string
  /** A secondary line (when), optional. */
  meta?: ComponentChildren
  content: string
  /** The keep button's label. */
  keepLabel: string
  /** Marks the panel currently shown to everyone. */
  shown?: boolean
  onKeep: () => void
}

export function PageConflictView({
  title, message, panels, onMerge, onCancel, busy = false, testId,
}: {
  title: string
  message: string
  panels: ConflictPanel[]
  onMerge: () => void
  /** Back out without choosing (federated conflicts only). */
  onCancel?: () => void
  busy?: boolean
  testId?: string
}) {
  return (
    <div class="sh-page-conflict" data-testid={testId}>
      <div class="sh-page-header">
        <h1>{title}</h1>
      </div>
      <p class="sh-conflict-banner" role="alert">{message}</p>
      <div
        class={`sh-conflict-panels${panels.length > 2 ? ' sh-conflict-panels--many' : ''}`}
      >
        {panels.map(p => (
          <section
            key={p.key}
            class={`sh-conflict-panel${p.shown ? ' sh-conflict-panel--shown' : ''}`}
            aria-label={p.heading}
          >
            <h3>{p.heading}</h3>
            {p.meta && <div class="sh-conflict-panel-meta sh-muted">{p.meta}</div>}
            <div class="sh-conflict-panel-body">
              <MarkdownView src={p.content} />
            </div>
            <Button onClick={p.onKeep} disabled={busy}>{p.keepLabel}</Button>
          </section>
        ))}
      </div>
      <div class="sh-form-actions">
        {onCancel && (
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            {t('common.cancel')}
          </Button>
        )}
        <Button variant="secondary" onClick={onMerge} disabled={busy}>
          {t('pages.conflict.merge')}
        </Button>
      </div>
    </div>
  )
}

/** The editor body for merging by hand: every version between markers. */
export function conflictMarkers(versions: { label: string; content: string }[]): string {
  if (versions.length === 0) return ''
  const [first, ...rest] = versions
  const parts = [`<<<<<<< ${first.label}`, first.content]
  for (const v of rest) parts.push(`======= ${v.label}`, v.content)
  parts.push('>>>>>>>')
  return `${parts.join('\n')}\n`
}
