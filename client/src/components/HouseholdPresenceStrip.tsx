/**
 * HouseholdPresenceStrip — small pinned-to-the-wall row of household
 * members shown above the household feed (§22 / §23.21).
 *
 * Each member is a polaroid-style card: avatar + display name + a
 * one-line status (``Home``, ``Away``, ``Not home`` or the active
 * zone name when ``state === 'zone'``). Cards stagger slight
 * rotations and carry a washi-tape strip at the top so the row reads
 * as a continuation of the feed's "taped to the wall" language.
 *
 * Click a card → navigate to ``/presence`` for the full detail page.
 *
 * The strip pulls the canonical presence list from ``/api/presence``
 * once on mount and merges live updates from the existing
 * ``presence`` WS-driven signal. Avatars are resolved through
 * ``householdUsers`` so picture changes from another tab show up
 * here for free.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { householdUsers } from '@/store/householdUsers'
import { presence, type PresenceEntry } from '@/store/presence'
import { Avatar } from './Avatar'

interface PresenceRow extends PresenceEntry {
  user_id: string
  display_name: string
}

function onlineState(row: PresenceRow): 'online' | 'idle' | null {
  if (!row.is_online) return null
  return row.is_idle ? 'idle' : 'online'
}

const rows = signal<PresenceRow[]>([])
const loaded = signal(false)

async function loadPresence(): Promise<void> {
  if (loaded.value) return
  loaded.value = true
  try {
    const data = await api.get('/api/presence') as PresenceRow[]
    rows.value = data
  } catch {
    loaded.value = false
  }
}

function statusLabel(row: PresenceRow): string {
  // ``zone`` state surfaces the human-readable zone name when the
  // server has one — otherwise fall back to a generic verb so we
  // never render an empty cell.
  if (row.state === 'zone' && row.zone_name) return row.zone_name
  switch (row.state) {
    case 'home': return 'Home'
    case 'away': return 'Away'
    case 'not_home': return 'Not home'
    case 'zone': return 'In zone'
    default: return row.state || 'Unknown'
  }
}

function statusModifier(state: string): string {
  // Drives the colored dot + tape tint per state. Names match the
  // existing presence-page CSS, so we get the same palette.
  switch (state) {
    case 'home': return 'sh-presence-pin--home'
    case 'zone': return 'sh-presence-pin--zone'
    case 'away': return 'sh-presence-pin--away'
    case 'not_home': return 'sh-presence-pin--away'
    default: return 'sh-presence-pin--unknown'
  }
}

export function HouseholdPresenceStrip() {
  const { route } = useLocation()
  useEffect(() => { void loadPresence() }, [])

  // Live-merge: every WS frame for a known user replaces that row.
  // Use the signal's reactivity directly — this component renders
  // again whenever ``presence.value`` changes.
  const live = presence.value
  const merged: PresenceRow[] = rows.value.map((r) =>
    live[r.username] ? { ...r, ...live[r.username] } : r,
  )

  if (merged.length === 0) return null

  return (
    <nav class="sh-presence-strip" aria-label="Household members">
      <div class="sh-presence-strip-inner">
        {merged.map((row, i) => {
          const userPic = householdUsers.value.get(row.user_id)?.picture_url
          return (
            <button
              key={row.user_id}
              type="button"
              class={`sh-presence-pin ${statusModifier(row.state)}`}
              style={{ '--sh-pin-rot': `${PIN_ROTATIONS[i % PIN_ROTATIONS.length]}deg` } as Record<string, string>}
              onClick={() => route('/presence')}
              aria-label={`${row.display_name} — ${statusLabel(row)}`}
            >
              <span class="sh-presence-pin-tape" aria-hidden="true" />
              <Avatar
                name={row.display_name}
                src={userPic}
                size={48}
                online={onlineState(row)}
              />
              <span class="sh-presence-pin-name">{row.display_name}</span>
              <span class="sh-presence-pin-status">
                <span class={`sh-presence-pin-dot sh-presence-pin-dot--${row.state || 'unknown'}`} aria-hidden="true" />
                {statusLabel(row)}
              </span>
            </button>
          )
        })}
      </div>
    </nav>
  )
}

/** Tiny rotation stagger so adjacent pins don't all tilt the same
 *  way. Cycles through 4 angles — keeps the row legible without
 *  feeling mechanical. */
const PIN_ROTATIONS = [-2, 1.5, -1, 2.5] as const
