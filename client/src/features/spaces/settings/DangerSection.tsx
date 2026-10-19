/**
 * Settings › Archive, hand over or delete — collapsed at the end of the
 * page so nobody lands on it by accident.
 *
 *   - Archive: read-only and hidden, undoable. A space the host dissolved
 *     or removed us from can't be unarchived (the server refuses).
 *   - Hand over ownership: the owner, on the hosting household only
 *     (``SpaceOwnershipTransfer``).
 *   - Delete: a proposal most admins must approve (v_16); with one admin
 *     it runs at once and the page leaves for the spaces list.
 */
import { useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { confirmDialog } from '@/components/confirm'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { clearLocalDissolve, markLocalDissolve } from '@/store/spaces'
import type { Space } from '@/types'
import { SpaceOwnershipTransfer } from '@/features/spaces/SpaceOwnershipTransfer'

export function DangerSection({ space, isOwner, isRemoteSpace, open, onUpdate }: {
  space: Space
  isOwner: boolean
  isRemoteSpace: boolean
  /** Opened by its own URL (``…/settings/danger``). */
  open: boolean
  onUpdate: () => void
}) {
  const { route } = useLocation()
  const [busy, setBusy] = useState(false)

  const setArchived = async (archived: boolean) => {
    setBusy(true)
    try {
      if (archived) await api.post(`/api/spaces/${space.id}/archive`)
      else await api.delete(`/api/spaces/${space.id}/archive`)
      showToast(
        archived ? t('space.archive.archived_toast') : t('space.archive.unarchived_toast'),
        'success',
      )
      onUpdate()
    } catch (e: unknown) {
      showToast((e as Error)?.message || t('space.archive.failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  const dissolve = async () => {
    const ok = await confirmDialog(t('space.dissolve.confirm_body'), {
      title: t('space.dissolve.confirm_title'),
      confirmLabel: t('space.dissolve.confirm_ok'),
      destructive: true,
    })
    if (!ok) return
    // A sole admin's dissolve executes inside this request, so the
    // server's ``dissolved`` frame arrives before the response: mark it
    // as ours so the live handlers leave the toast + redirect to us.
    markLocalDissolve(space.id)
    try {
      const res = await api.post<{ proposal?: { status?: string } }>(
        `/api/spaces/${space.id}/proposals`, { action: 'dissolve' },
      )
      if (res?.proposal?.status === 'executed') {
        showToast(t('space.dissolve.done'), 'info')
        route('/spaces', true)
      } else {
        // Only proposed: when the other admins approve later, the
        // dissolve frame must still move this tab off the space.
        clearLocalDissolve(space.id)
        showToast(t('space.dissolve.proposed'), 'info')
      }
    } catch (e: unknown) {
      clearLocalDissolve(space.id)
      showToast((e as Error)?.message || t('space.dissolve.failed'), 'error')
    }
  }

  return (
    <details class="sh-ss-danger" open={open} data-testid="settings-danger">
      <summary class="sh-ss-danger__summary">
        <span class="sh-ss-danger__title">{t('space.settings_page.danger.summary')}</span>
        <span class="sh-ss-danger__meta">{t('space.settings_page.danger.meta')}</span>
      </summary>
      <div class="sh-ss-danger__body">
        <section aria-labelledby={`archive-h-${space.id}`}>
          <h3 class="sh-ss-subheading" id={`archive-h-${space.id}`}>{t('space.archive.heading')}</h3>
          {space.archived ? (
            space.archived_reason === 'dissolved' || space.archived_reason === 'removed' ? (
              <p class="sh-ss-hint">
                <strong>
                  {space.archived_reason === 'dissolved'
                    ? t('space.archive.dissolved_lead') : t('space.archive.removed_lead')}
                </strong>{' '}
                {t('space.archive.readonly_copy')}
              </p>
            ) : (
              <>
                <p class="sh-ss-hint">
                  <strong>{t('space.archive.archived_lead')}</strong>{' '}
                  {t('space.archive.archived_body')}
                </p>
                <Button variant="secondary" loading={busy} onClick={() => void setArchived(false)}>
                  {t('space.archive.unarchive')}
                </Button>
              </>
            )
          ) : (
            <>
              <p class="sh-ss-hint">{t('space.archive.intro')}</p>
              <Button variant="secondary" loading={busy} onClick={() => void setArchived(true)}>
                {t('space.archive.archive')}
              </Button>
            </>
          )}
        </section>

        {isOwner && !isRemoteSpace && (
          <section aria-labelledby={`transfer-h-${space.id}`}>
            <h3 class="sh-ss-subheading" id={`transfer-h-${space.id}`}>
              {t('space.transfer.heading')}
            </h3>
            <SpaceOwnershipTransfer spaceId={space.id} spaceName={space.name}
                                    onTransferred={onUpdate} />
          </section>
        )}

        <section aria-labelledby={`dissolve-h-${space.id}`}>
          <h3 class="sh-ss-subheading" id={`dissolve-h-${space.id}`}>
            {t('space.settings_page.danger.delete')}
          </h3>
          <p class="sh-ss-hint">{t('space.dissolve.intro')}</p>
          <Button variant="danger" onClick={() => void dissolve()}>{t('space.dissolve.button')}</Button>
        </section>
      </div>
    </details>
  )
}
