/**
 * PairingSteps — "How pairing works", shown instead of the household list
 * while there are no households yet.
 */
import { t } from '@/i18n/i18n'
import { SectionHeader } from './SectionHeader'

const STEPS = [
  ['connections.steps.meet_title', 'connections.steps.meet_body'],
  ['connections.steps.scan_title', 'connections.steps.scan_body'],
  ['connections.steps.done_title', 'connections.steps.done_body'],
] as const

export function PairingSteps() {
  return (
    <>
      <SectionHeader id="sh-conn-steps" focusable title={t('connections.steps.title')} />
      <ol class="sh-conn-steps" aria-labelledby="sh-conn-steps">
        {STEPS.map(([title, body]) => (
          <li key={title} class="sh-conn-step">
            <strong>{t(title)}</strong>
            {t(body)}
          </li>
        ))}
      </ol>
    </>
  )
}
