/**
 * ExternalAddressRow — the federation inbox base URL peers POST to.
 *
 * Rendered only when a manual address is honoured: under the Home
 * Assistant App the companion integration owns the value (a hand-typed
 * URL would point at an inbox path only that integration registers), and
 * the server says the same as ``manual_supported: false``. The page hides
 * the row in both cases.
 *
 * The summary shows what is in effect (``effective``) — it differs from
 * the stored value when this setup's config also provides one.
 */
import { useEffect, useState } from 'preact/hooks'
import { t, isOne } from '@/i18n/i18n'
import { showToast } from '@/components/Toast'
import { AdvancedRow, setAdvancedOpen } from './AdvancedRow'
import { ConnChip } from './ConnChip'
import { externalUrl, saveExternalUrl } from './connectionsData'
import { hostOf } from './model'

export function ExternalAddressRow() {
  const ext = externalUrl.value
  const [draft, setDraft] = useState(ext.stored ?? '')
  const [busy, setBusy] = useState(false)

  // Follow the server value whenever it changes — during render, not in an
  // effect, so an effect landing late can't overwrite what was just typed.
  const [syncedFrom, setSyncedFrom] = useState(ext.stored)
  if (syncedFrom !== ext.stored) {
    setSyncedFrom(ext.stored)
    setDraft(ext.stored ?? '')
  }
  // Not set → the row starts open: it is the thing to fix.
  useEffect(() => {
    if (ext.loaded && !ext.effective) setAdvancedOpen('ext', true)
  }, [ext.loaded, ext.effective])

  const save = async () => {
    setBusy(true)
    try {
      const r = await saveExternalUrl(draft.trim() ? draft.trim() : null)
      showToast(
        r.base === null
          ? t('connections.ext.cleared')
          : r.peers_notified > 0
            ? t(isOne(r.peers_notified) ? 'connections.ext.saved_notified_one' : 'connections.ext.saved_notified',
              { n: String(r.peers_notified) })
            : t('connections.ext.saved'),
        'success',
      )
    } catch (err) {
      showToast(
        (err as { status?: number })?.status === 422
          ? t('connections.ext.invalid')
          : t('connections.ext.failed'),
        'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const dirty = draft.trim() !== (ext.stored ?? '')
  const desc = !ext.effective
    ? t('connections.adv.ext_desc_unset')
    : ext.source === 'auto'
      ? t('connections.adv.ext_desc_auto', { host: hostOf(ext.effective) })
      : hostOf(ext.effective)
  const chip = !ext.effective
    ? <ConnChip tone="warn">{t('connections.adv.chip_not_set')}</ConnChip>
    : ext.source === 'auto'
      ? <ConnChip tone="ok">{t('connections.adv.chip_from_setup')}</ConnChip>
      : <ConnChip tone="ok">{t('connections.adv.chip_set')}</ConnChip>

  return (
    <AdvancedRow id="ext" icon="addr" label={t('connections.ext.title')} desc={desc} chip={chip}>
      <p>{t('connections.ext.intro')}</p>
      <div class="sh-conn-field">
        <label for="sh-conn-ext-input">{t('connections.ext.label')}</label>
        <div class="sh-conn-field__row">
          <input
            id="sh-conn-ext-input"
            class="sh-input"
            type="url"
            inputMode="url"
            autocomplete="off"
            placeholder="https://home.example.com"
            value={draft}
            disabled={busy}
            onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
          />
          <button
            type="button"
            class="sh-btn sh-btn--primary"
            disabled={busy || !dirty}
            onClick={() => void save()}
          >
            {busy ? t('connections.ext.saving') : t('common.save')}
          </button>
        </div>
      </div>
      {ext.effective && (
        <p class="sh-conn-wrap">{t('connections.ext.effective', { url: ext.effective })}</p>
      )}
      {ext.stored && <p>{t('connections.ext.clear_hint')}</p>}
    </AdvancedRow>
  )
}
