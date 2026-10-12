/**
 * SetupWarning — the first-run honey line in the hero: nobody can reach
 * this household yet (no external address in effect and no GFS), so a
 * pairing would fail. Links to the External address row only when that
 * row exists — under the Home Assistant App (or when the server refuses a
 * manual address) the line points at "Add a GFS" alone.
 */
import { t } from '@/i18n/i18n'
import { ConnIcon } from './ConnIcon'
import { fillNodes } from './richText'

interface Props {
  /** Opens + focuses the External address row; omitted when hidden. */
  onExternalAddress?: () => void
  onAddGfs: () => void
}

export function SetupWarning({ onExternalAddress, onAddGfs }: Props) {
  const link = (label: string, onClick: () => void) => (
    <button type="button" class="sh-conn-inline-link" onClick={onClick}>{label}</button>
  )
  return (
    <p class="sh-conn-hero__warn" role="note">
      <ConnIcon name="alert" />
      <span>
        {onExternalAddress
          ? fillNodes(t('connections.hero.setup_warn'), {
            ext: link(t('connections.hero.setup_warn_ext'), onExternalAddress),
            gfs: link(t('connections.hero.setup_warn_gfs'), onAddGfs),
          })
          : fillNodes(t('connections.hero.setup_warn_gfs_only'), {
            gfs: link(t('connections.hero.setup_warn_gfs_cap'), onAddGfs),
          })}
      </span>
    </p>
  )
}
