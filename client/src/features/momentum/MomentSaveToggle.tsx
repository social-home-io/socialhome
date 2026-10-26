/**
 * MomentSaveToggle — Save / Saved on someone's moment (decision 6b).
 *
 * A saved moment stays with you past its end until its author deletes it.
 * The server says per item whether you saved it (``saved_by_me``) and
 * whether Save is offered (``can_save`` — only where the author's delete
 * can still reach your copy, never on your own moments). The toggle shows
 * when either is true, so a save can always be removed.
 *
 * A toggle button (``aria-pressed``): ``PUT`` / ``DELETE
 * /api/moments/{id}/save``, a toast either way. Two looks — ``chip`` (the
 * inbox row's engagement strip) and ``button`` (the moment's page).
 */
import { useEffect } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import type { Moment } from '@/types'

interface Props {
  moment: Pick<Moment, 'id' | 'saved_by_me' | 'can_save'>
  variant?: 'chip' | 'button'
  /** After the server agreed — the host may refresh a list. */
  onChange?: (saved: boolean) => void
}

export function MomentSaveToggle({ moment, variant = 'button', onChange }: Props) {
  const saved = useSignal<boolean>(moment.saved_by_me === true)
  const busy = useSignal<boolean>(false)
  // A refetch of the host list carries the server's word.
  useEffect(() => { saved.value = moment.saved_by_me === true }, [saved, moment.id, moment.saved_by_me])

  if (!saved.value && moment.can_save !== true) return null

  const toggle = async (ev: Event) => {
    // The inbox row opens the moment on click; the toggle mustn't.
    ev.preventDefault()
    ev.stopPropagation()
    if (busy.value) return
    busy.value = true
    const next = !saved.value
    try {
      if (next) await api.put(`/api/moments/${moment.id}/save`, {})
      else await api.delete(`/api/moments/${moment.id}/save`)
      saved.value = next
      showToast(t(next ? 'moment.save.toast_saved' : 'moment.save.toast_removed'), 'success')
      onChange?.(next)
    } catch (err: unknown) {
      const error = String((err as Error)?.message ?? err)
      showToast(t(next ? 'moment.save.failed' : 'moment.save.remove_failed', { error }), 'error')
    } finally {
      busy.value = false
    }
  }

  const label = t(saved.value ? 'moment.save.saved' : 'moment.save.save')
  if (variant === 'chip') {
    return (
      <button
        type="button"
        class={saved.value ? 'sh-momentum-chip sh-momentum-chip--saved' : 'sh-momentum-chip'}
        aria-pressed={saved.value}
        disabled={busy.value}
        onClick={toggle}
      >
        <span aria-hidden="true">🔖</span>
        <span class="sh-momentum-chip__label">{label}</span>
      </button>
    )
  }
  return (
    <button
      type="button"
      class={saved.value ? 'sh-btn sh-btn--secondary sh-momentum-save is-saved' : 'sh-btn sh-btn--ghost sh-momentum-save'}
      aria-pressed={saved.value}
      disabled={busy.value}
      onClick={toggle}
    >
      <span aria-hidden="true">🔖</span> {label}
    </button>
  )
}
