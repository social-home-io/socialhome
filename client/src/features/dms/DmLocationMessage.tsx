/**
 * DmLocationMessage — the bubble body of a ``type='location'`` DM.
 *
 * Parses the message ``content`` (JSON, see ``utils/dmLocation``) and
 * renders the shared location card — map thumbnail through the tile
 * proxy, label, 4-dp coordinates, coarse accuracy and "Open in maps".
 * Content from another household is untrusted: anything that doesn't
 * parse as an in-range pin renders a muted "couldn't be shown" line
 * instead of a map (and never the raw JSON).
 */
import { t } from '@/i18n/i18n'
import { LocationPostCard } from '@/components/LocationPostCard'
import { parseDmLocation } from '@/utils/dmLocation'

export function DmLocationMessage({ content }: { content: string }) {
  const loc = parseDmLocation(content)
  if (!loc) {
    return (
      <p class="sh-muted sh-dm-location-unreadable" style={{ margin: 0 }}>
        📍 {t('location.unreadable')}
      </p>
    )
  }
  return (
    <div class="sh-dm-location">
      <LocationPostCard location={loc} />
    </div>
  )
}
