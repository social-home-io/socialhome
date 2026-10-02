/**
 * AccessNote — where a feature's create / edit controls would be, the
 * short "Only admins can add pages here" note a member or moderator sees
 * in an ADMIN_ONLY feature (§4.3).
 */
import { accessNote, type AccessFeature } from './spaceAccess'

export function AccessNote({ feature }: { feature: AccessFeature }) {
  return (
    <p class="sh-access-note" role="note" data-testid={`access-note-${feature}`}>
      <span aria-hidden="true">🔒</span>
      <span>{accessNote(feature)}</span>
    </p>
  )
}
