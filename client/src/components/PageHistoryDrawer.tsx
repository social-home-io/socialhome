/**
 * PageHistoryDrawer — right-side drawer showing edit history + diff.
 *
 * Fetches ``versionsUrl`` (``/api/pages/{id}/versions`` or
 * ``/api/spaces/{sid}/pages/{pid}/versions``; newest first after sort),
 * renders a per-entry card with editor + timestamp, and shows an inline
 * line-level diff between the selected version and the current page
 * body. With a ``revertUrl`` (household admins) a "Restore" button POSTs
 * it; space pages have no revert route, so their history is read-only.
 *
 * The diff is a small local LCS — O(n·m) on line counts. Fine for
 * Markdown pages (spec §2627 caps body at 4000 chars, so < ~100 lines).
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { normaliseTimestamp } from '@/utils/relativeTime'
import { Button } from './Button'
import { showToast } from './Toast'
import type { PageVersion } from '@/types'
import { confirmDialog } from '@/components/confirm'

interface Props {
  /** ``GET`` — the page's versions. */
  versionsUrl: string
  /** ``POST {version}`` restores one; ``null`` → read-only history. */
  revertUrl: string | null
  /** Shown instead of the Restore button when ``revertUrl`` is null. */
  revertNote: string
  /** An editor's name (``edited_by`` is a user id). */
  nameOf: (uid: string) => string
  currentContent: string
  open: boolean
  onClose: () => void
  /** Called after a successful revert so the parent can reload the page. */
  onRestored: (newContent: string) => void
}

type DiffLine = { kind: 'same' | 'add' | 'del', text: string }

/** Tiny LCS-based line diff — not byte-perfect but good enough for
 * reviewing a Markdown page. Returns rows with {kind, text}. */
function diffLines(a: string, b: string): DiffLine[] {
  const A = a.split('\n')
  const B = b.split('\n')
  const m = A.length, n = B.length
  // LCS table
  const L: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0))
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1])
    }
  }
  const out: DiffLine[] = []
  let i = 0, j = 0
  while (i < m && j < n) {
    if (A[i] === B[j]) { out.push({ kind: 'same', text: A[i] }); i++; j++ }
    else if (L[i + 1][j] >= L[i][j + 1]) { out.push({ kind: 'del', text: A[i] }); i++ }
    else { out.push({ kind: 'add', text: B[j] }); j++ }
  }
  while (i < m) { out.push({ kind: 'del', text: A[i++] }) }
  while (j < n) { out.push({ kind: 'add', text: B[j++] }) }
  return out
}

export function PageHistoryDrawer(
  { versionsUrl, revertUrl, revertNote, nameOf, currentContent, open, onClose, onRestored }: Props,
) {
  const [versions, setVersions] = useState<PageVersion[]>([])
  const [selected, setSelected] = useState<PageVersion | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    void api.get(versionsUrl).then((rows: PageVersion[]) => {
      const sorted = [...rows].sort((x, y) => y.version - x.version)
      setVersions(sorted)
      setSelected(sorted[0] ?? null)
    }).catch(() => {
      showToast(t('pages.history.load_failed'), 'error')
    })
  }, [open, versionsUrl])

  if (!open) return null

  const restore = async () => {
    if (!selected || busy || !revertUrl) return
    const version = String(selected.version)
    if (!await confirmDialog(t('pages.history.restore_confirm', { version }), { destructive: true })) return
    setBusy(true)
    try {
      const resp = await api.post(
        revertUrl, { version: selected.version },
      ) as { content: string }
      showToast(t('pages.history.restored', { version }), 'success')
      onRestored(resp.content ?? selected.content)
      onClose()
    } catch (err: unknown) {
      showToast(t('pages.history.restore_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      setBusy(false)
    }
  }

  const rows = selected ? diffLines(selected.content, currentContent) : []

  return (
    <aside class="sh-history-drawer" role="dialog" aria-label={t('pages.history.title')}>
      <div class="sh-history-drawer-header">
        <h3 style={{ margin: 0 }}>{t('pages.history.title')}</h3>
        <button
          type="button" class="sh-modal-close"
          aria-label={t('pages.history.close')} onClick={onClose}
        >×</button>
      </div>
      <div class="sh-history-drawer-body">
        {versions.length === 0 && (
          <p class="sh-muted">{t('pages.history.empty')}</p>
        )}
        {versions.map(v => (
          <button
            type="button"
            key={v.id}
            class={`sh-history-entry ${selected?.id === v.id ? 'sh-history-entry--active' : ''}`}
            aria-pressed={selected?.id === v.id}
            onClick={() => setSelected(v)}
          >
            <div>
              <strong>v{v.version}</strong>
              {' '}
              <span class="sh-muted">{v.title}</span>
            </div>
            <div class="sh-history-entry-meta">
              <span>{nameOf(v.edited_by)}</span>
              <span>·</span>
              <span>{new Date(normaliseTimestamp(v.edited_at)).toLocaleString()}</span>
            </div>
          </button>
        ))}

        {selected && (
          <>
            <h4 style={{ marginTop: '1rem' }}>
              {t('pages.history.diff_heading', { version: String(selected.version) })}
            </h4>
            <div class="sh-history-diff" aria-label={t('pages.history.diff_label')}>
              {rows.length === 0 && <em class="sh-muted">{t('pages.history.no_changes')}</em>}
              {rows.map((r, idx) => (
                <div
                  key={idx}
                  class={r.kind === 'add' ? 'sh-diff-add' : r.kind === 'del' ? 'sh-diff-del' : 'sh-diff-same'}
                >
                  {r.kind === 'add' ? '+ ' : r.kind === 'del' ? '- ' : '  '}
                  {r.text || '\u00A0'}
                </div>
              ))}
            </div>
            {revertUrl ? (
              <div class="sh-form-actions" style={{ marginTop: '0.75rem' }}>
                <Button variant="primary" loading={busy} onClick={restore}>
                  {t('pages.history.restore')}
                </Button>
              </div>
            ) : (
              <p class="sh-muted" style={{ marginTop: '0.75rem' }}>
                {revertNote}
              </p>
            )}
          </>
        )}
      </div>
    </aside>
  )
}
