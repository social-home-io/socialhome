/* Media cache card — the GFS's encrypted, short-lived space media cache,
   from the ``space_media`` block of ``GET /admin/api/overview``. Totals come
   from the shared database (the whole cluster); evictions and refusals are
   this node's since it started. The admin portal is English-only. */

export interface SpaceMediaStats {
  enabled: boolean
  stored_blobs: number
  stored_bytes: number
  max_bytes: number
  ttl_seconds: number
  evicted_blobs: number
  evicted_bytes: number
  refused_quota: number
  refused_too_large: number
}

const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB']

const oneDecimal = (v: number) => Math.round(v * 10) / 10

/** Bytes in binary units: below 10 of a unit one decimal (``1.5 MiB``)
 *  unless whole (``5 GiB``), larger ones none (``256 MiB``). Rounded first:
 *  a value that rounds to 1024 is the next unit (``1.0 MiB``, never
 *  ``1024 KiB``) and one that rounds to 10 drops its decimal (``10 MiB``). */
export function formatBytes(bytes: number): string {
  let value = Math.max(0, bytes)
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  let shown = value < 10 ? oneDecimal(value) : Math.round(value)
  if (shown >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
    shown = oneDecimal(value)
  }
  if (unit === 0) return `${Math.round(value)} B`
  const text = shown < 10 && !Number.isInteger(value) ? shown.toFixed(1) : String(Math.round(shown))
  return `${text} ${UNITS[unit]}`
}

/** A TTL in the largest unit that divides it: days, hours, else minutes. */
export function formatTtl(seconds: number): string {
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`
  if (seconds % 86_400 === 0) return plural(seconds / 86_400, 'day')
  if (seconds % 3_600 === 0) return plural(seconds / 3_600, 'hour')
  return plural(Math.round(seconds / 60), 'minute')
}

const count = (n: number) => n.toLocaleString('en-US')

export function MediaCacheCard({ stats }: { stats: SpaceMediaStats }) {
  if (!stats.enabled || stats.max_bytes <= 0) {
    return (
      <section class="card media-cache" aria-labelledby="media-cache-title">
        <h3 id="media-cache-title">Media cache</h3>
        <p class="media-cache-off">
          <strong>Turned off.</strong> Households don't upload space pictures to this GFS.
        </p>
        <p class="muted">
          To turn it on, set <code>[space_media] max_bytes</code> (or{' '}
          <code>GFS_SPACE_MEDIA_MAX_BYTES</code>) above 0.
        </p>
      </section>
    )
  }
  const used = formatBytes(stats.stored_bytes)
  const max = formatBytes(stats.max_bytes)
  const usedText = `${used} of ${max} used`
  const percent = Math.min(100, Math.round((stats.stored_bytes / stats.max_bytes) * 100))
  // A cache in use never looks empty: a sliver when it rounds to 0 %.
  const fill = stats.stored_bytes > 0 ? Math.max(1, percent) : 0
  return (
    <section class="card media-cache" aria-labelledby="media-cache-title">
      <h3 id="media-cache-title">Media cache</h3>
      <p class="media-cache-used">{usedText}</p>
      <div
        class="media-cache-bar"
        role="progressbar"
        aria-label="Media cache used"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={usedText}
      >
        <span style={{ width: `${fill}%` }} />
      </div>
      <dl class="media-cache-facts">
        <dt>Pictures and files</dt>
        <dd>{count(stats.stored_blobs)}</dd>
        <dt>Kept for</dt>
        <dd>{formatTtl(stats.ttl_seconds)} at most</dd>
      </dl>
      <h4 class="media-cache-node">Since this node started</h4>
      <dl class="media-cache-facts">
        <dt>Evicted to make room</dt>
        <dd>
          {count(stats.evicted_blobs)} ({formatBytes(stats.evicted_bytes)})
        </dd>
        <dt>Refused</dt>
        <dd>
          <span class="media-cache-phrase">
            {count(stats.refused_quota)} over a space's upload rate
          </span>{' '}
          ·{' '}
          <span class="media-cache-phrase">{count(stats.refused_too_large)} too large</span>
        </dd>
      </dl>
    </section>
  )
}
