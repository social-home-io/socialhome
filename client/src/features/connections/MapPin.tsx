/**
 * Map pins for the Connections map — the same ConnAvatar a household has
 * in the list (hash tint, status ring + glyph badge), with its name on a
 * chip underneath, and a hearth "You" disc for this household.
 *
 * Leaflet wants an HTML string for a DivIcon, so :func:`pinHtml` renders
 * the component into a detached node and serialises it — Preact writes
 * every peer-supplied value as text, so a name like ``<img>`` stays inert.
 */
import { render } from 'preact'
import type { VNode } from 'preact'
import { t } from '@/i18n/i18n'
import type { Connection } from '@/store/connections'
import { ConnAvatar, type AvatarStatus } from './ConnAvatar'
import { initialOf, isSpaceOnly, tintFor } from './model'

/** Pin box: the avatar is 44 px; the icon is anchored at its centre and
 *  the name chip hangs below the box. */
export const PIN_SIZE = 44
export const SELF_SIZE = 48

/** Status a pin shows when the page doesn't say otherwise. Version data
 *  is admin-only, so ``warn`` only ever comes from the page. */
export function defaultPinStatus(peer: Connection): AvatarStatus | undefined {
  if (isSpaceOnly(peer)) return undefined
  if (peer.status && peer.status !== 'confirmed') return 'pending'
  return peer.reachable === false ? 'down' : 'ok'
}

export function PeerPin({ peer, status }: { peer: Connection; status?: AvatarStatus }) {
  const name = peer.display_name || peer.instance_id
  return (
    <div class="sh-conn-pin" data-status={status ?? 'none'}>
      <ConnAvatar letter={initialOf(name)} tint={tintFor(peer.instance_id)} status={status} />
      <span class="sh-conn-pin__label">{name}</span>
    </div>
  )
}

export function SelfPin() {
  return (
    <div class="sh-conn-pin sh-conn-pin--you">
      <span class="sh-conn-pin__you">{t('connections.map.you_pin')}</span>
    </div>
  )
}

/** Serialise a pin for ``L.divIcon({ html })``. */
export function pinHtml(node: VNode): string {
  const el = document.createElement('div')
  render(node, el)
  const html = el.innerHTML
  render(null, el)
  return html
}
