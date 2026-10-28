/**
 * HaAutomationsSwitch — Settings → Notifications: the person's opt-in to
 * have their notifications and their own activity (DMs they get, their
 * posts, tasks, events, listings, status, presence) fired on Home
 * Assistant's event bus as `socialhome.*` events for automations (D14).
 *
 * Off by default. Shown only when the platform forwards events to an
 * automation engine (`automation_events` capability — ha + haos) and the
 * account is not protected. The server stays the authority: a protected
 * account gets `403 ACCOUNT_PROTECTED` / `ha_automations` on
 * `PATCH /api/me`, and the event bridge skips it whatever is stored.
 */
import { useSignal } from '@preact/signals'
import { t } from '@/i18n/i18n'
import { supportsAutomationEvents } from '@/platform'
import { isRestricted } from '@/components/ProtectedNotice'
import { showToast } from '@/components/Toast'
import { getPreferences, setPreference } from '@/utils/preferences'

/** Whether this person is offered the switch at all. */
export function haAutomationsOffered(): boolean {
  return supportsAutomationEvents() && !isRestricted('ha_automations')
}

export function HaAutomationsSwitch() {
  const busy = useSignal(false)
  if (!haAutomationsOffered()) return null
  const on = getPreferences().ha_automation_events === true

  const toggle = async () => {
    busy.value = true
    try {
      await setPreference('ha_automation_events', !on)
    } catch (e: unknown) {
      showToast((e as Error).message || t('settings.ha_automations.save_failed'), 'error')
    } finally {
      busy.value = false
    }
  }

  return (
    <div class="sh-settings-subcard" data-testid="ha-automations">
      <h3>{t('settings.ha_automations.title')}</h3>
      <label class="sh-toggle-row">
        <input
          type="checkbox"
          checked={on}
          disabled={busy.value}
          aria-describedby="sh-ha-automations-hint"
          onChange={() => void toggle()}
        />
        {t('settings.ha_automations.label')}
      </label>
      <p id="sh-ha-automations-hint" class="sh-muted">
        {t('settings.ha_automations.hint')}
      </p>
    </div>
  )
}
