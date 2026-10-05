/**
 * BazaarCreateDialog — multi-step listing creation (§23.15 / §23.25).
 *
 * Step 1: pick a space + title + description + images
 * Step 2: mode + price fields + currency + duration
 * Submit: POST /api/bazaar
 *
 * Listings are space-scoped — the wrapper post lives inside the picked
 * space, so visibility / federation follow the space's rules.
 */
import { signal } from '@preact/signals'
import { useEffect, useRef } from 'preact/hooks'
import { api } from '@/api'
import { contentWrite } from '@/utils/contentWrite'
import { Modal } from './Modal'
import { Button } from './Button'
import { ProtectedNotice, isRestricted } from './ProtectedNotice'
import { isOne, t } from '@/i18n/i18n'
import { showToast } from './Toast'
import { uploadWithProgress, UploadProgressBar } from './UploadProgress'
import { describeUploadError } from '@/utils/uploadErrors'
import type { BazaarMode, Space } from '@/types'

const ZERO_DECIMAL_CURRENCIES: ReadonlySet<string> =
  new Set(['JPY', 'KRW', 'ISK'])
const CURRENCIES = [
  'EUR','USD','GBP','CHF','SEK','NOK','DKK','PLN','CZK',
  'JPY','CAD','AUD','NZD','SGD','HKD',
]

const MAX_IMAGES = 5

const open = signal(false)
const step = signal(1)
const title = signal('')
const description = signal('')
const mode = signal<BazaarMode>('fixed')
const price = signal('')
const startPrice = signal('')
const stepPrice = signal('')
const currency = signal('EUR')
const durationDays = signal(7)
interface ImageEntry { url: string; preview: string }
const imageUrls = signal<ImageEntry[]>([])
const submitting = signal(false)
const availableSpaces = signal<Space[]>([])
const spacesLoading = signal(false)
const spaceId = signal<string>('')
// When opened from a space's Bazaar tab the target space is fixed — the
// picker is replaced by a static label so the seller can't retarget.
const lockedSpaceId = signal<string | null>(null)
// Opt-in feed announcement (§23.15). Off by default: listings live in the
// Bazaar tab and only surface in the feed when the seller asks.
const announceInFeed = signal(false)

function reset() {
  step.value = 1
  title.value = ''
  description.value = ''
  mode.value = 'fixed'
  price.value = ''
  startPrice.value = ''
  stepPrice.value = ''
  currency.value = 'EUR'
  durationDays.value = 7
  imageUrls.value = []
  submitting.value = false
  spaceId.value = ''
  lockedSpaceId.value = null
  announceInFeed.value = false
}

export function openBazaarCreate(presetSpaceId?: string) {
  reset()
  if (presetSpaceId) {
    spaceId.value = presetSpaceId
    lockedSpaceId.value = presetSpaceId
  }
  open.value = true
}

function toCents(raw: string, cur: string): number | null {
  if (!raw.trim()) return null
  const n = Number(raw)
  if (!Number.isFinite(n) || n < 0) return null
  return ZERO_DECIMAL_CURRENCIES.has(cur) ? Math.round(n) : Math.round(n * 100)
}

async function loadSpaces() {
  spacesLoading.value = true
  try {
    const rows = await api.get('/api/spaces') as Space[]
    // Bazaar listings need a real "post target" space. ``global`` /
    // ``public`` discovery rows are read-only browse surfaces, so
    // exclude them from the picker.
    availableSpaces.value = rows.filter(
      (s) => s.space_type === 'private' || s.space_type === 'household',
    )
    if (!spaceId.value && availableSpaces.value.length > 0) {
      spaceId.value = availableSpaces.value[0].id
    }
  } catch {
    availableSpaces.value = []
  } finally {
    spacesLoading.value = false
  }
}

export function BazaarCreateDialog({ onCreated }: { onCreated?: () => void }) {
  const fileRef = useRef<HTMLInputElement | null>(null)
  useEffect(() => {
    if (open.value) void loadSpaces()
  }, [open.value])

  const uploadImage = async (file: File) => {
    try {
      // Store the **canonical** url (no ``?exp=&sig=``); the bazaar
      // listing endpoint persists ``image_urls`` and the server signs
      // them fresh on every read. ``signed_url`` is for the immediate
      // preview only — see UploadProgress.uploadWithProgress.
      const result = await uploadWithProgress(file)
      imageUrls.value = [
        ...imageUrls.value,
        { url: result.url, preview: result.signed_url },
      ].slice(0, MAX_IMAGES)
    } catch (err: unknown) {
      showToast(describeUploadError(err, { file }), 'error')
    }
  }

  const onFilesPicked = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const files = Array.from(input.files ?? [])
    for (const f of files) {
      if (imageUrls.value.length >= MAX_IMAGES) break
      await uploadImage(f)
    }
    input.value = ''
  }

  const removeImage = (url: string) => {
    imageUrls.value = imageUrls.value.filter(u => u.url !== url)
  }

  const submit = async () => {
    if (!spaceId.value) {
      showToast(t('bazaar.create.pick_space'), 'error')
      return
    }
    const body: Record<string, unknown> = {
      space_id:      spaceId.value,
      title:         title.value.trim(),
      description:   description.value.trim() || undefined,
      mode:          mode.value,
      currency:      currency.value,
      duration_days: durationDays.value,
      image_urls:    imageUrls.value.map((e) => e.url),
      announce_in_feed: announceInFeed.value,
    }
    const priceC = toCents(price.value, currency.value)
    const startC = toCents(startPrice.value, currency.value)
    const stepC  = toCents(stepPrice.value,  currency.value)
    if (mode.value === 'fixed' || mode.value === 'negotiable') {
      if (priceC == null || priceC <= 0) {
        showToast(t('bazaar.create.invalid_price'), 'error')
        return
      }
      body.price = priceC
    }
    if (mode.value === 'auction' || mode.value === 'bid_from') {
      if (startC == null || startC <= 0) {
        showToast(t('bazaar.create.invalid_start_price'), 'error')
        return
      }
      body.start_price = startC
      if (stepC != null) body.step_price = stepC
    }
    submitting.value = true
    try {
      // The listing's feed post may be held for review (posts
      // "Reviewed", §4.3) — then nothing is listed yet; ``contentWrite``
      // toasts that.
      const res = await contentWrite(api.post('/api/bazaar', body), { spaceId: spaceId.value })
      if (!res.queued) showToast(t('bazaar.create.created'), 'success')
      open.value = false
      if (!res.queued) onCreated?.()
    } catch (err: unknown) {
      showToast(
        t('bazaar.create.failed', { error: String((err as Error).message ?? err) }), 'error',
      )
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal open={open.value}
           onClose={() => { open.value = false }}
           title={t('bazaar.new_listing')}>
      {isRestricted('bazaar') && <ProtectedNotice capability="bazaar" />}
      {!isRestricted('bazaar') && step.value === 1 && (
        <div class="sh-form sh-bazaar-create">
          {lockedSpaceId.value ? (
            <label>
              {t('bazaar.create.space')}
              <span class="sh-muted">
                {(() => {
                  const s = availableSpaces.value.find(
                    (x) => x.id === lockedSpaceId.value,
                  )
                  return s ? `${s.emoji ? `${s.emoji} ` : ''}${s.name}` : t('bazaar.create.this_space')
                })()}
              </span>
            </label>
          ) : (
            <label>
              {t('bazaar.create.space')} *
              {spacesLoading.value ? (
                <span class="sh-muted">{t('bazaar.create.loading_spaces')}</span>
              ) : availableSpaces.value.length === 0 ? (
                <span class="sh-muted">
                  {t('bazaar.create.no_spaces')}
                </span>
              ) : (
                <select
                  value={spaceId.value}
                  onChange={(e) => {
                    spaceId.value = (e.target as HTMLSelectElement).value
                  }}
                >
                  {availableSpaces.value.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.emoji ? `${s.emoji} ` : ''}{s.name}
                    </option>
                  ))}
                </select>
              )}
            </label>
          )}
          <label>
            {t('bazaar.create.title')} *
            <input value={title.value} maxLength={200}
              onInput={(e) => title.value = (e.target as HTMLInputElement).value} />
          </label>
          <label>
            {t('bazaar.create.description')}
            <textarea value={description.value} rows={4} maxLength={2000}
              onInput={(e) => description.value = (e.target as HTMLTextAreaElement).value} />
          </label>

          <div>
            <strong style={{ fontSize: 'var(--sh-font-size-sm)' }}>{t('bazaar.create.photos')}</strong>
            <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)', margin: '2px 0 8px' }}>
              {t('bazaar.create.photos_hint', { n: String(MAX_IMAGES) })}
            </p>
            <div class="sh-bazaar-create-images">
              {imageUrls.value.map(entry => (
                <div key={entry.url} class="sh-bazaar-create-img">
                  <img src={entry.preview} alt="" />
                  <button type="button" class="sh-composer-remove-attach"
                          aria-label={t('bazaar.create.remove_image')}
                          onClick={() => removeImage(entry.url)}>✕</button>
                </div>
              ))}
              {imageUrls.value.length < MAX_IMAGES && (
                <button type="button" class="sh-bazaar-create-add"
                        onClick={() => fileRef.current?.click()}>
                  <span>＋</span>
                  <span>{t('composer.add_photo')}</span>
                </button>
              )}
              <input ref={fileRef} type="file" accept="image/*" multiple
                     class="sr-only"
                     onChange={onFilesPicked} />
            </div>
            <UploadProgressBar />
          </div>

          <div class="sh-form-actions">
            <Button variant="secondary"
                    onClick={() => { open.value = false }}>
              {t('common.cancel')}
            </Button>
            <Button onClick={() => (step.value = 2)}
                    disabled={!title.value.trim() || !spaceId.value}>
              {t('bazaar.create.next')}
            </Button>
          </div>
        </div>
      )}
      {step.value === 2 && (
        <div class="sh-form sh-bazaar-create">
          <label>
            {t('bazaar.create.mode')}
            <select value={mode.value}
                    onChange={(e) =>
                      mode.value = (e.target as HTMLSelectElement).value as BazaarMode}>
              <option value="fixed">{t('bazaar.create.mode_fixed')}</option>
              <option value="offer">{t('bazaar.create.mode_offer')}</option>
              <option value="auction">{t('bazaar.create.mode_auction')}</option>
              <option value="bid_from">{t('bazaar.create.mode_bid_from')}</option>
              <option value="negotiable">{t('bazaar.create.mode_negotiable')}</option>
            </select>
          </label>

          <label>
            {t('bazaar.create.currency')}
            <select value={currency.value}
                    onChange={(e) =>
                      currency.value = (e.target as HTMLSelectElement).value}>
              {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>

          {(mode.value === 'fixed' || mode.value === 'negotiable') && (
            <label>
              {t('bazaar.create.price')}
              <input type="number" step="0.01" min="0" value={price.value}
                onInput={(e) => price.value = (e.target as HTMLInputElement).value} />
            </label>
          )}

          {(mode.value === 'auction' || mode.value === 'bid_from') && (
            <>
              <label>
                {t('bazaar.create.start_price')}
                <input type="number" step="0.01" min="0" value={startPrice.value}
                  onInput={(e) => startPrice.value = (e.target as HTMLInputElement).value} />
              </label>
              <label>
                {t('bazaar.create.step_price')}
                <input type="number" step="0.01" min="0" value={stepPrice.value}
                  placeholder={t('bazaar.create.step_price_placeholder')}
                  onInput={(e) => stepPrice.value = (e.target as HTMLInputElement).value} />
              </label>
            </>
          )}

          <label>
            {t('bazaar.create.duration')}
            <select value={String(durationDays.value)}
                    onChange={(e) =>
                      durationDays.value = parseInt(
                        (e.target as HTMLSelectElement).value,
                      ) || 7}>
              {[1, 3, 5, 7].map(d => (
                <option key={d} value={String(d)}>
                  {t(isOne(d) ? 'bazaar.create.days_one' : 'bazaar.create.days', { n: String(d) })}
                </option>
              ))}
            </select>
          </label>

          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={announceInFeed.value}
              onChange={(e) =>
                announceInFeed.value = (e.target as HTMLInputElement).checked}
            />
            {t('bazaar.create.announce')}
          </label>

          <div class="sh-form-actions">
            <Button variant="secondary" onClick={() => (step.value = 1)}>
              ← {t('common.back')}
            </Button>
            <Button onClick={submit} loading={submitting.value}>
              {t('bazaar.create.submit')}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  )
}
