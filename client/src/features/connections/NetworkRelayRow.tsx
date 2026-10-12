/**
 * NetworkRelayRow — the STUN/TURN servers the federation transport uses to
 * punch through NAT, secrets stripped.
 *
 * Fetched on mount, not on open: the chip is the point — an admin whose
 * TURN entry got wiped has no reason to open a collapsed row, so the
 * warning must be visible first. A missing or credential-less TURN entry
 * otherwise degrades direct connections to the slow path in silence.
 * Per-mount state: the list changes underneath us (an HA pull replaces
 * it daily), so coming back to the page refetches.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { AdvancedRow } from './AdvancedRow'
import { ConnChip, type ChipTone } from './ConnChip'

interface IceOverview {
  servers: { urls: string[]; kinds: string[]; has_credentials: boolean }[]
  has_turn: boolean
  turn_usable: boolean
  pulls_from_home_assistant: boolean
}

type Verdict = 'ready' | 'direct_only' | 'broken' | 'unavailable'

const CHIP: Record<Verdict, { tone: ChipTone; key: string }> = {
  ready: { tone: 'ok', key: 'connections.adv.relay_ready' },
  direct_only: { tone: 'warn', key: 'connections.adv.relay_direct_only' },
  broken: { tone: 'warn', key: 'connections.adv.relay_broken' },
  unavailable: { tone: 'muted', key: 'connections.adv.relay_unavailable' },
}

export function iceVerdict(data: IceOverview | null): Verdict {
  if (data === null) return 'unavailable'
  if (data.turn_usable) return 'ready'
  return data.has_turn ? 'broken' : 'direct_only'
}

export function NetworkRelayRow() {
  const [loaded, setLoaded] = useState(false)
  const [data, setData] = useState<IceOverview | null>(null)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const overview = await api.get('/api/admin/federation/ice-servers') as IceOverview
        if (!cancelled) setData(overview)
      } catch {
        if (!cancelled) setData(null)
      } finally {
        if (!cancelled) setLoaded(true)
      }
    })()
    return () => { cancelled = true }
  }, [])

  const verdict = loaded ? iceVerdict(data) : null
  return (
    <AdvancedRow
      id="relay"
      icon="relay"
      label={t('connections.adv.relay_title')}
      desc={t('connections.adv.relay_desc')}
      chip={verdict && <ConnChip tone={CHIP[verdict].tone}>{t(CHIP[verdict].key)}</ConnChip>}
    >
      {!loaded && <p>{t('connections.ice.loading')}</p>}
      {loaded && data === null && <p>{t('connections.ice.read_failed')}</p>}
      {loaded && data !== null && (
        <>
          {data.servers.length === 0 ? (
            <p>{t('connections.ice.none')}</p>
          ) : (
            <ul class="sh-conn-ice">
              {data.servers.map((s) => (
                <li key={s.urls.join(',')}>
                  <code>{s.urls.join(', ')}</code>
                  <span>
                    {s.kinds.includes('turn') || s.kinds.includes('turns')
                      ? s.has_credentials
                        ? t('connections.ice.kind_backup_signed_in')
                        : t('connections.ice.kind_backup_no_login')
                      : t('connections.ice.kind_lookup')}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p>
            {!data.has_turn
              ? t('connections.ice.hint_no_turn')
              : !data.turn_usable
                ? t('connections.ice.hint_turn_broken')
                : t('connections.ice.hint_ready')}
            {data.pulls_from_home_assistant && t('connections.ice.from_ha')}
          </p>
        </>
      )}
    </AdvancedRow>
  )
}
