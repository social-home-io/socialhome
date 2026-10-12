/**
 * MapFoot — under the map (never over it, so pins and the OSM credit stay
 * visible): the status legend and which households aren't on the map,
 * with a "Show list" way back. "Needs an update" is admin-only data.
 */
import { t, isOne, formatLocale } from '@/i18n/i18n'
import { ConnIcon } from './ConnIcon'
import { fillNodes, joinNames } from './richText'

interface Props {
  isAdmin: boolean
  /** Display names of rows without a shared home location. */
  hidden: string[]
  onShowList: () => void
}

export function MapFoot({ isAdmin, hidden, onShowList }: Props) {
  const showList = (
    <button type="button" class="sh-link" onClick={onShowList}>{t('connections.map.show_list')}</button>
  )
  return (
    <div class="sh-conn-map__foot">
      <ul class="sh-conn-map__legend" aria-label={t('connections.map.legend')}>
        <li><span class="sh-conn-lg sh-conn-lg--ok" aria-hidden="true" />{t('connections.map.lg_ok')}</li>
        {isAdmin && (
          <li><span class="sh-conn-lg sh-conn-lg--warn" aria-hidden="true"><ConnIcon name="up" /></span>{t('connections.map.lg_warn')}</li>
        )}
        <li><span class="sh-conn-lg sh-conn-lg--down" aria-hidden="true"><ConnIcon name="bang" /></span>{t('connections.map.lg_down')}</li>
      </ul>
      <p class="sh-conn-map__note">
        <ConnIcon name="info" />
        <span>
          {t('connections.map.only_shared')}
          {hidden.length > 0 && (
            <>
              {' '}
              {fillNodes(
                t(isOne(hidden.length) ? 'connections.map.not_shown_one' : 'connections.map.not_shown'),
                { names: joinNames(hidden, formatLocale()) },
              )}
            </>
          )}
          {' '}
          {showList}
        </span>
      </p>
    </div>
  )
}
