/**
 * HouseholdId — a household's ID, the fingerprint of its identity key
 * (``instance_id``: 32 base32 characters), shown so two people can
 * compare it.
 *
 * One format everywhere it appears — a paired household's "Technical
 * details", this household's own ID on the Connections page, and the
 * check before an admin grant (:mod:`AdminGrantCheck`): the whole ID in
 * groups of four. The groups are set apart with CSS only, so selecting
 * or copying the text gives the ID itself, without spaces.
 */
import { ConnIcon } from '@/features/connections/ConnIcon'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

const GROUP = 4

function groupsOf(id: string): string[] {
  const out: string[] = []
  for (let i = 0; i < id.length; i += GROUP) out.push(id.slice(i, i + GROUP))
  return out
}

async function copyId(id: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(id)
    showToast(t('connections.manage.copied'), 'success')
  } catch {
    showToast(t('connections.manage.copy_failed'), 'error')
  }
}

export function HouseholdId({ id, copy = true }: {
  id: string
  /** Offer a "Copy household ID" button next to the ID. */
  copy?: boolean
}) {
  return (
    <span class="sh-household-id">
      <code class="sh-household-id__code" data-testid="household-id">
        {groupsOf(id).map((g, i) => <span key={i}>{g}</span>)}
      </code>
      {copy && (
        <button type="button" class="sh-conn-copy"
                aria-label={t('connections.manage.copy_id')}
                onClick={() => void copyId(id)}>
          <ConnIcon name="copy" />
        </button>
      )}
    </span>
  )
}
