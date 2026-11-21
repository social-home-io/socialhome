/**
 * createLiveThrottle — run ``fn`` at most once per ``minGapMs``, with one
 * trailing call for a burst.
 *
 * Live WS frames come in bursts (a busy space chat, a room of people
 * viewing a highlight). A page that refetches per frame turns each burst
 * into a request storm; this keeps it to one call now (when the last one
 * is long enough ago) and one trailing call at the end of the gap — so
 * the last state always lands, never more than one call per gap.
 *
 *  - ``startAt``: count a call at that time (a page's own load at mount),
 *    so the first frame right after it waits for the gap. Default: none.
 *  - ``pauseWhileHidden``: nothing runs while the tab is hidden; a call
 *    missed there runs once when the page shows again.
 *
 * ``flush()`` runs a pending trailing call now (leaving a page); ``cancel()``
 * drops it and stops listening.
 */

export interface LiveThrottleOptions {
  minGapMs: number
  startAt?: number | null
  pauseWhileHidden?: boolean
}

export interface LiveThrottle {
  trigger(): void
  flush(): void
  cancel(): void
}

export function createLiveThrottle(
  fn: () => void,
  { minGapMs, startAt = null, pauseWhileHidden = false }: LiveThrottleOptions,
): LiveThrottle {
  let last: number | null = startAt
  let timer: ReturnType<typeof setTimeout> | null = null
  let missedWhileHidden = false

  const isHidden = () => pauseWhileHidden && typeof document !== 'undefined' && document.hidden

  const fire = () => {
    if (isHidden()) {
      missedWhileHidden = true
      return
    }
    last = Date.now()
    fn()
  }
  const trigger = () => {
    if (isHidden()) {
      missedWhileHidden = true
      return
    }
    if (timer) return  // a trailing call is already due
    const wait = last === null ? 0 : last + minGapMs - Date.now()
    if (wait <= 0) {
      fire()
      return
    }
    timer = setTimeout(() => { timer = null; fire() }, wait)
  }
  const onVisibility = () => {
    if (document.hidden || !missedWhileHidden) return
    missedWhileHidden = false
    trigger()
  }
  if (pauseWhileHidden && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibility)
  }

  return {
    trigger,
    flush() {
      if (!timer) return
      clearTimeout(timer)
      timer = null
      fire()
    },
    cancel() {
      if (timer) clearTimeout(timer)
      timer = null
      missedWhileHidden = false
      if (pauseWhileHidden && typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility)
      }
    },
  }
}
