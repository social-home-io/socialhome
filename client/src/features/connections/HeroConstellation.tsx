/**
 * HeroConstellation — the decorative network sketch in the hero:
 * "You" in the middle, one node per household (problems first, so they
 * are always drawn), a single GFS tile when any GFS is connected, and
 * three dashed "+" slots on first run. ``aria-hidden`` and static — the
 * headline, chips and cards carry the same information as text.
 */
import { t } from '@/i18n/i18n'
import { connGlyph } from './ConnIcon'
import {
  CX, CY, GFS_TILE, emptySlots, layoutConstellation, linkPath,
} from './constellationLayout'
import { initialOf, tintFor, type Household } from './model'

interface Props {
  households: Household[]
  gfs: boolean
  /** First run: draw the dashed "+" slots. */
  empty?: boolean
}

export function HeroConstellation({ households, gfs, empty = false }: Props) {
  const { nodes, more } = layoutConstellation(households, gfs)
  return (
    <svg
      class="sh-conn-constellation"
      viewBox="0 0 248 184"
      aria-hidden="true"
      focusable="false"
    >
      <circle class="orbit" cx={CX} cy={CY} r="52" />
      <circle class="orbit" cx={CX} cy={CY} r="82" />
      {nodes.map(({ item, x, y }) => (
        <path key={`l-${item.conn.instance_id}`} class={`link link--${item.status}`} d={linkPath(x, y)} />
      ))}
      {gfs && (
        <path class="link link--gfs" d={linkPath(GFS_TILE.x, GFS_TILE.y, -6)} />
      )}
      {empty && emptySlots().map(({ x, y }, i) => (
        <g key={`e-${i}`}>
          <path class="link link--pending" d={`M${CX},${CY} L${x},${y}`} />
          <circle class="node node--empty" cx={x} cy={y} r="17" />
          <text class="plus" x={x} y={y}>+</text>
        </g>
      ))}
      {nodes.map(({ item, x, y }) => (
        <g key={`n-${item.conn.instance_id}`} data-status={item.status}>
          <circle class={`ring--${item.status}`} cx={x} cy={y} r="22" />
          <circle class={`node node--${tintFor(item.conn.instance_id)}`} cx={x} cy={y} r="18" />
          <text x={x} y={y}>{initialOf(item.conn.display_name)}</text>
        </g>
      ))}
      {more && (
        <g data-more={more.count}>
          <circle class="node node--ink" cx={more.x} cy={more.y} r="18" />
          <text class="cap" x={more.x} y={more.y}>+{more.count}</text>
        </g>
      )}
      {gfs && (
        <>
          <g transform={`translate(${GFS_TILE.x - 14},${GFS_TILE.y - 14})`}>
            <rect width="28" height="28" rx="9" class="node node--honey" />
            <g transform="translate(5,4) scale(0.75)" class="gfs-glyph">{connGlyph('cloud')}</g>
          </g>
          <text class="cap" x={GFS_TILE.x} y={GFS_TILE.y + 25}>GFS</text>
        </>
      )}
      <circle class="node node--you" cx={CX} cy={CY} r="25" />
      <text class="you-label" x={CX} y={CY}>{t('connections.map.you_pin')}</text>
    </svg>
  )
}
