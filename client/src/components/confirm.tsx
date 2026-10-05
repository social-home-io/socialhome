/**
 * confirmDialog — promise-based wrapper around ``<ConfirmDialog>`` so
 * destructive-action sites stay one-liners.
 *
 * Usage:
 * ```ts
 *   if (!await confirmDialog('Delete this post?', { destructive: true })) return
 * ```
 *
 * Replaces the native ``window.confirm()`` whose modal can't be styled
 * and breaks the washi-tape aesthetic. Same UX intent, same control
 * flow, just pulled through the project's ``Modal`` chrome with focus
 * trap, Escape-to-cancel, and a typed danger button.
 *
 * Mount ``<ConfirmDialogHost />`` once at the app root next to the
 * other global dialogs. Only one confirm prompt is shown at a time —
 * if a second ``confirmDialog(...)`` is called while one is open, the
 * older one resolves to ``false`` and the new one takes over.
 */
import { signal } from '@preact/signals'
import { ConfirmDialog } from './ConfirmDialog'
import { t } from '@/i18n/i18n'

interface ConfirmOpts {
  /** Title shown at the top of the modal. Default: "Are you sure?". */
  title?: string
  /** Label for the confirm button. Default: "Confirm". */
  confirmLabel?: string
  /** Label for the cancel button. Default: "Cancel". */
  cancelLabel?: string
  /** When true, the confirm button uses the danger variant. */
  destructive?: boolean
}

interface PendingConfirm extends ConfirmOpts {
  message: string
  resolve: (ok: boolean) => void
}

const pending = signal<PendingConfirm | null>(null)


/** Open a confirm dialog. Resolves ``true`` on confirm, ``false`` on
 *  cancel / Escape / overlay click. */
export function confirmDialog(
  message: string,
  opts: ConfirmOpts = {},
): Promise<boolean> {
  return new Promise(resolve => {
    // If a previous prompt is still open, drop it (caller resolves
    // false). This matches the native ``window.confirm`` behaviour
    // where a second call while one is open is a no-op for the first.
    const prev = pending.value
    if (prev) prev.resolve(false)
    pending.value = { message, ...opts, resolve }
  })
}


/** Mount once at the app root. Renders the live ConfirmDialog when a
 *  ``confirmDialog(...)`` promise is pending; renders nothing
 *  otherwise. */
export function ConfirmDialogHost() {
  const p = pending.value
  if (!p) return null
  const close = (ok: boolean) => {
    pending.value = null
    p.resolve(ok)
  }
  return (
    <ConfirmDialog
      open={true}
      title={p.title ?? t('confirm.default_title')}
      message={p.message}
      confirmLabel={p.confirmLabel}
      cancelLabel={p.cancelLabel}
      destructive={p.destructive}
      onConfirm={() => close(true)}
      onCancel={() => close(false)}
    />
  )
}
