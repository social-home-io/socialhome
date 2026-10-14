/**
 * ShareHomeToggle — admin-only toggle that controls whether our household's
 * home pin is visible on this peer's Connections → Map (§23.90).
 *
 * Optimistic local-state flip + PATCH to /api/pairing/connections/{id},
 * revert + toast on error.
 */
import { useState } from 'preact/hooks'
import { api } from '@/api'
import { showToast } from './Toast'
import { Switch } from './Switch'
import { t } from '@/i18n/i18n'

export interface ShareHomeToggleProps {
  instanceId: string
  peerName: string
  initialValue: boolean
  onChange?: (newValue: boolean) => void
  /** ``row`` (default): checkbox row, as in the pairing flow. ``card``: a
   *  setting card with a switch, as in the Manage sheet. */
  variant?: 'row' | 'card'
}

export function ShareHomeToggle({
  instanceId,
  peerName,
  initialValue,
  onChange,
  variant = 'row',
}: ShareHomeToggleProps) {
  const [enabled, setEnabled] = useState(initialValue)

  const toggle = async () => {
    const next = !enabled
    // Optimistic flip
    setEnabled(next)
    try {
      await api.patch(`/api/pairing/connections/${encodeURIComponent(instanceId)}`, { share_home: next })
      onChange?.(next)
    } catch (e: any) {
      // Revert on error
      setEnabled(!next)
      showToast(e.message || t('share_home.failed'), 'error')
    }
  }

  if (variant === 'card') {
    const titleId = `sh-share-home-title-${instanceId}`
    const helpId = `sh-share-home-help-${instanceId}`
    return (
      <label class="sh-conn-setting sh-share-home-toggle">
        <span class="sh-conn-setting__text">
          <b id={titleId}>{t('connections.manage.share_home_title')}</b>
          <span id={helpId}>
            {t(enabled ? 'connections.manage.share_home_on' : 'connections.manage.share_home_off', { peer: peerName })}
          </span>
        </span>
        <Switch
          checked={enabled}
          aria-labelledby={titleId}
          aria-describedby={helpId}
          onChange={() => void toggle()}
        />
      </label>
    )
  }

  const helpText = enabled
    ? t('share_home.on', { peer: peerName })
    : t('share_home.off', { peer: peerName })

  return (
    <div class="sh-share-home-toggle">
      <label class="sh-toggle-row">
        <input
          type="checkbox"
          checked={enabled}
          onChange={() => void toggle()}
        />
        {t('share_home.label', { peer: peerName })}
      </label>
      <p class="sh-muted sh-share-home-toggle__help" style={{ marginTop: 'var(--sh-space-xs)', fontSize: 'var(--sh-font-size-sm)' }}>
        {helpText}
      </p>
    </div>
  )
}
