/**
 * CalendarImport — add events to a household calendar in bulk (§5.2).
 *
 *   1. From a file (.ics)        → POST /api/calendars/{id}/import_ics
 *      (raw ``text/calendar`` body)
 *   2. From a photo (AI)         → POST /api/calendars/{id}/import_image
 *      (raw ``image/*`` body, optional ``?caption=``)
 *   3. From a description (AI)   → POST /api/calendars/{id}/import_prompt
 *      (JSON ``{prompt}``)
 *
 * All three answer ``201 {events: [...], created, updated}`` — the rows
 * written, and how many were new vs. updated in place. ICS import is
 * all-or-nothing: one VEVENT without SUMMARY / DTSTART fails the whole
 * file with ``422 ICS_PARSE_ERROR`` and nothing is written, so the
 * result is either a count or one error — never a partial import. The
 * server keys each VEVENT on its ``UID`` (+ ``RECURRENCE-ID``), so
 * re-importing the same file updates its events instead of adding them
 * again; events removed from the file are left on the calendar.
 *
 * Request bodies are capped at 1 MiB (aiohttp's ``client_max_size``), so
 * files are size-checked before upload and large photos are downscaled.
 * The AI paths only render when the platform exposes the ``ai``
 * capability (HA / HAOS with an ``ai_task`` backend).
 */
import { signal, useSignal } from '@preact/signals'
import { api, ApiError } from '@/api'
import { supportsAi } from '@/platform'
import { currentUser } from '@/store/auth'
import { householdUsers } from '@/store/householdUsers'
import { Button } from './Button'
import { FormError } from './FormError'
import { Modal } from './Modal'
import { Spinner } from './Spinner'
import { t, isOne, formatLocale } from '@/i18n/i18n'

export interface ImportCalendarOption {
  id: string
  name: string
  owner_username: string
}

export interface ImportedEvent {
  id: string
  summary: string
  start: string
  all_day?: boolean
}

type Source = 'file' | 'photo' | 'text'

/** Request-body ceiling (aiohttp's default ``client_max_size``). */
export const MAX_UPLOAD_BYTES = 1024 * 1024
/** Longest edge a photo is downscaled to before the AI import. */
const PHOTO_MAX_EDGE = 1600
const SAMPLE_COUNT = 5

const isOpen = signal(false)

/** Open the import dialog from anywhere on the Calendar page (the
 *  header button, the empty state). */
export function openCalendarImport(): void {
  isOpen.value = true
}

export function closeCalendarImport(): void {
  isOpen.value = false
}

interface Props {
  calendars: ImportCalendarOption[]
  /** The caller's own calendar — the default target. */
  defaultCalendarId: string | null
  /** Resolve a calendar to write into when the caller has none yet. */
  ensureCalendar: () => Promise<string>
  /** Called after events were created, so the page can refresh. */
  onImported: (calendarId: string, events: ImportedEvent[]) => void
  /** Jump the calendar to the imported events (they are often in
   *  another month than the one on screen). Omit to show only "Done". */
  onShow?: (events: ImportedEvent[]) => void
}

/** The dialog host. Mount it once at page level — NOT inside a toolbar
 *  that re-renders into a different branch (the calendar filter strip
 *  swaps layouts when visibility changes), or the dialog remounts and
 *  drops its result the moment an import switches a calendar on. Open
 *  it with ``openCalendarImport()``. */
export function CalendarImport(props: Props) {
  if (!isOpen.value) return null
  return <CalendarImportDialog {...props} onClose={closeCalendarImport} />
}

function ownerLabel(owner: string): string {
  if (owner === currentUser.value?.username) return t('calendar.page.owner_you')
  for (const u of householdUsers.value.values()) {
    if (u.username === owner) return u.display_name || u.username
  }
  return owner
}

function calendarLabel(c: ImportCalendarOption, all: ImportCalendarOption[]): string {
  const ambiguous = all.filter(o => o.owner_username === c.owner_username).length > 1
  return ambiguous ? `${ownerLabel(c.owner_username)} — ${c.name}` : ownerLabel(c.owner_username)
}

function formatWhen(ev: ImportedEvent): string {
  const ts = Date.parse(ev.start)
  if (Number.isNaN(ts)) return ''
  return ev.all_day
    // All-day imports are anchored to UTC midnight (floating date).
    ? new Date(ts).toLocaleDateString(formatLocale(), { dateStyle: 'medium', timeZone: 'UTC' })
    : new Date(ts).toLocaleString(formatLocale(), { dateStyle: 'medium', timeStyle: 'short' })
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Map a failed import to a message that says what to do next. */
export function importErrorMessage(e: unknown, source: Source): string {
  if (e instanceof ApiError) {
    if (e.status === 413) return t('calendar.import.err_too_big')
    if (e.status === 503) return t('calendar.import.err_ai_unavailable')
    if (e.code === 'ICS_PARSE_ERROR') {
      return t('calendar.import.err_parse', { detail: e.detail ?? t('calendar.import.err_parse_default') })
    }
    if (e.code === 'AI_PARSE_ERROR') {
      return source === 'photo'
        ? t('calendar.import.err_ai_photo')
        : t('calendar.import.err_ai_text')
    }
    if (e.status === 404) return t('calendar.import.err_gone')
    if (e.detail) return e.message
  }
  if (e instanceof TypeError) {
    return t('calendar.import.err_network')
  }
  return (e as Error)?.message || t('calendar.import.err_generic')
}

/** Downscale a photo that is over the upload ceiling. Returns the
 *  original file when it already fits or the browser can't decode it. */
async function preparePhoto(file: File): Promise<Blob> {
  if (file.size <= MAX_UPLOAD_BYTES) return file
  try {
    const bmp = await createImageBitmap(file)
    const scale = Math.min(1, PHOTO_MAX_EDGE / Math.max(bmp.width, bmp.height))
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(bmp.width * scale)
    canvas.height = Math.round(bmp.height * scale)
    canvas.getContext('2d')?.drawImage(bmp, 0, 0, canvas.width, canvas.height)
    bmp.close()
    const blob = await new Promise<Blob | null>(res => canvas.toBlob(res, 'image/jpeg', 0.85))
    return blob ?? file
  } catch {
    return file
  }
}

interface Result {
  calendarId: string
  events: ImportedEvent[]
  /** Rows newly added / updated in place (a re-imported ``UID``). */
  created: number
  updated: number
  source: Source
}

interface ImportResponse {
  events: ImportedEvent[]
  created?: number
  updated?: number
}

/** The result headline. Plain "Added N" when nothing was updated; the
 *  added / updated split once a re-import touched existing events. */
export function importHeadline(r: Pick<Result, 'events' | 'created' | 'updated'>, targetPhrase: string): string {
  const n = r.events.length
  if (r.updated === 0) {
    return t(isOne(n) ? 'calendar.import.added_one' : 'calendar.import.added', {
      n: String(n), target: targetPhrase,
    })
  }
  return t(isOne(n) ? 'calendar.import.imported_one' : 'calendar.import.imported', {
    n: String(n), target: targetPhrase, created: String(r.created), updated: String(r.updated),
  })
}

function CalendarImportDialog({
  calendars, defaultCalendarId, ensureCalendar, onImported, onShow, onClose,
}: Props & { onClose: () => void }) {
  const aiAvailable = supportsAi()
  const source = useSignal<Source>('file')
  const target = useSignal<string | null>(defaultCalendarId ?? calendars[0]?.id ?? null)
  const file = useSignal<File | null>(null)
  const photo = useSignal<File | null>(null)
  const caption = useSignal('')
  const prompt = useSignal('')
  const busy = useSignal(false)
  const error = useSignal<string | null>(null)
  const result = useSignal<Result | null>(null)

  const reset = () => {
    file.value = null
    photo.value = null
    caption.value = ''
    prompt.value = ''
    error.value = null
    result.value = null
  }

  const pickFile = async (f: File | null) => {
    error.value = null
    file.value = null
    if (!f) return
    if (f.size > MAX_UPLOAD_BYTES) {
      error.value = t('calendar.import.err_file_size', { size: formatSize(f.size) })
      return
    }
    // Cheap sniff so a wrong file (a PDF, a CSV) fails here with a clear
    // message instead of a parser error from the server. ``trimStart``
    // also drops a UTF-8 BOM (U+FEFF counts as whitespace).
    const head = (await f.slice(0, 4096).text()).trimStart()
    if (!/^BEGIN:VCALENDAR/i.test(head)) {
      error.value = t('calendar.import.err_not_ics')
      return
    }
    file.value = f
  }

  const submit = async (e: Event) => {
    e.preventDefault()
    error.value = null
    busy.value = true
    const src = source.value
    try {
      const calendarId = target.value ?? await ensureCalendar()
      const base = `/api/calendars/${encodeURIComponent(calendarId)}`
      let res: ImportResponse
      if (src === 'file') {
        if (!file.value) { error.value = t('calendar.import.err_no_file'); return }
        res = await api.postRaw(`${base}/import_ics`, file.value, 'text/calendar')
      } else if (src === 'photo') {
        if (!photo.value) { error.value = t('calendar.import.err_no_photo'); return }
        const blob = await preparePhoto(photo.value)
        if (blob.size > MAX_UPLOAD_BYTES) {
          error.value = t('calendar.import.err_photo_size')
          return
        }
        const note = caption.value.trim()
        const qs = note ? `?${new URLSearchParams({ caption: note })}` : ''
        res = await api.postRaw(`${base}/import_image${qs}`, blob, blob.type || 'image/jpeg')
      } else {
        const text = prompt.value.trim()
        if (!text) { error.value = t('calendar.import.err_no_text'); return }
        res = await api.post(`${base}/import_prompt`, { prompt: text })
      }
      const events = res?.events ?? []
      // The AI paths (and an older server) omit the split — every row
      // is then a new one.
      const updated = typeof res?.updated === 'number' ? res.updated : 0
      const created = typeof res?.created === 'number' ? res.created : events.length - updated
      result.value = { calendarId, events, created, updated, source: src }
      onImported(calendarId, events)
    } catch (err) {
      error.value = importErrorMessage(err, src)
    } finally {
      busy.value = false
    }
  }

  const done = result.value
  const targetCal = calendars.find(c => c.id === (done?.calendarId ?? target.value))
  const targetPhrase = !targetCal || targetCal.owner_username === currentUser.value?.username
    ? t('calendar.import.target_yours')
    : t('calendar.import.target_other', { name: calendarLabel(targetCal, calendars) })
  const canSubmit = !busy.value && (
    source.value === 'file' ? file.value !== null
      : source.value === 'photo' ? photo.value !== null
        : prompt.value.trim().length > 0
  )

  return (
    <Modal open onClose={onClose} title={t('calendar.import.title')}>
      {done ? (
        <div class="sh-cal-import-result" role="status">
          <p class="sh-cal-import-result__headline">
            {importHeadline(done, targetPhrase)}
          </p>
          <ul class="sh-cal-import-result__list">
            {done.events.slice(0, SAMPLE_COUNT).map(ev => (
              <li key={ev.id}>
                <strong>{ev.summary}</strong>
                <span class="sh-muted"> · {formatWhen(ev)}</span>
              </li>
            ))}
          </ul>
          {done.events.length > SAMPLE_COUNT && (
            <p class="sh-muted">{t('calendar.import.more', { n: String(done.events.length - SAMPLE_COUNT) })}</p>
          )}
          {done.source === 'file' && (
            <p class="sh-muted sh-cal-import-result__note">
              {t('calendar.import.reimport_note')}
            </p>
          )}
          <div class="sh-form-actions">
            <Button variant="secondary" onClick={reset}>{t('calendar.import.more_button')}</Button>
            {onShow && done.events.length > 0 ? (
              <Button onClick={() => { onShow(done.events); onClose() }}>
                {t('calendar.import.show')}
              </Button>
            ) : (
              <Button onClick={onClose}>{t('pairing.done')}</Button>
            )}
          </div>
        </div>
      ) : (
        <form class="sh-form sh-cal-import" onSubmit={submit} noValidate>
          {calendars.length > 1 && (
            <label>
              {t('calendar.import.add_to')}
              <select
                value={target.value ?? ''}
                onChange={(e) => { target.value = (e.target as HTMLSelectElement).value }}
              >
                {calendars.map(c => (
                  <option key={c.id} value={c.id}>{calendarLabel(c, calendars)}</option>
                ))}
              </select>
            </label>
          )}

          {aiAvailable && (
            <div class="sh-cal-import-sources" role="radiogroup" aria-label={t('calendar.import.from_aria')}>
              {([
                ['file', t('calendar.import.src_file')],
                ['photo', t('calendar.import.src_photo')],
                ['text', t('calendar.import.src_text')],
              ] as [Source, string][]).map(([value, label]) => (
                <label key={value} class={
                  'sh-cal-import-source'
                  + (source.value === value ? ' sh-cal-import-source--on' : '')
                }>
                  <input
                    type="radio"
                    name="sh-cal-import-source"
                    value={value}
                    checked={source.value === value}
                    onChange={() => { source.value = value; error.value = null }}
                  />
                  {label}
                </label>
              ))}
            </div>
          )}

          {source.value === 'file' && (
            <label class="sh-cal-import-file">
              {t('calendar.import.src_file')}
              <span class="sh-form-help">
                {t('calendar.import.file_help')}
              </span>
              <input
                type="file"
                accept=".ics,.ical,.icalendar,.vcs,text/calendar"
                aria-describedby={error.value ? 'sh-cal-import-error' : undefined}
                onChange={(e) => {
                  void pickFile((e.target as HTMLInputElement).files?.[0] ?? null)
                }}
              />
              {file.value && (
                <span class="sh-muted sh-cal-import-file__picked">
                  {file.value.name} · {formatSize(file.value.size)}
                </span>
              )}
            </label>
          )}

          {source.value === 'photo' && (
            <>
              <label>
                {t('calendar.import.photo_label')}
                <span class="sh-form-help">
                  {t('calendar.import.photo_help')}
                </span>
                <input
                  type="file"
                  accept="image/*"
                  onChange={(e) => {
                    photo.value = (e.target as HTMLInputElement).files?.[0] ?? null
                    error.value = null
                  }}
                />
              </label>
              <label>
                {t('calendar.import.note_label')}
                <input
                  type="text"
                  maxLength={200}
                  placeholder={t('calendar.import.note_placeholder')}
                  value={caption.value}
                  onInput={(e) => { caption.value = (e.target as HTMLInputElement).value }}
                />
              </label>
            </>
          )}

          {source.value === 'text' && (
            <label>
              {t('calendar.import.text_label')}
              <textarea
                rows={4}
                maxLength={2000}
                placeholder={t('calendar.import.text_placeholder')}
                value={prompt.value}
                onInput={(e) => { prompt.value = (e.target as HTMLTextAreaElement).value }}
              />
            </label>
          )}

          <FormError id="sh-cal-import-error" message={error.value} />
          {busy.value && (
            <div class="sh-cal-import-busy" aria-live="polite">
              <Spinner size={6} label={t('calendar.import.busy_aria')} /> <span>{t('calendar.import.busy')}</span>
            </div>
          )}

          <div class="sh-form-actions">
            <Button type="button" variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" loading={busy.value} disabled={!canSubmit}>{t('calendar.page.import')}</Button>
          </div>
        </form>
      )}
    </Modal>
  )
}
