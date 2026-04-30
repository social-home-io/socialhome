/**
 * BazaarCreateDialog — multi-step listing creation (§23.15 / §23.25).
 *
 * Step 1: title + description + images (multi-upload to /api/media/upload)
 * Step 2: mode + price fields + currency + duration
 * Submit: POST /api/bazaar
 */
import { signal } from '@preact/signals'
import { useRef } from 'preact/hooks'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import { uploadWithProgress, UploadProgressBar } from './UploadProgress'
import type { BazaarMode } from '@/types'

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
}

export function openBazaarCreate() {
  reset()
  open.value = true
}

function toCents(raw: string, cur: string): number | null {
  if (!raw.trim()) return null
  const n = Number(raw)
  if (!Number.isFinite(n) || n < 0) return null
  return ZERO_DECIMAL_CURRENCIES.has(cur) ? Math.round(n) : Math.round(n * 100)
}

export function BazaarCreateDialog({ onCreated }: { onCreated?: () => void }) {
  const fileRef = useRef<HTMLInputElement | null>(null)

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
      showToast(
        `Image upload failed: ${(err as Error).message ?? err}`, 'error',
      )
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
    const body: Record<string, unknown> = {
      title:         title.value.trim(),
      description:   description.value.trim() || undefined,
      mode:          mode.value,
      currency:      currency.value,
      duration_days: durationDays.value,
      image_urls:    imageUrls.value.map((e) => e.url),
    }
    const priceC = toCents(price.value, currency.value)
    const startC = toCents(startPrice.value, currency.value)
    const stepC  = toCents(stepPrice.value,  currency.value)
    if (mode.value === 'fixed' || mode.value === 'negotiable') {
      if (priceC == null || priceC <= 0) {
        showToast('Enter a valid price', 'error')
        return
      }
      body.price = priceC
    }
    if (mode.value === 'auction' || mode.value === 'bid_from') {
      if (startC == null || startC <= 0) {
        showToast('Enter a valid starting price', 'error')
        return
      }
      body.start_price = startC
      if (stepC != null) body.step_price = stepC
    }
    submitting.value = true
    try {
      await api.post('/api/bazaar', body)
      showToast('Listing created', 'success')
      open.value = false
      onCreated?.()
    } catch (err: unknown) {
      showToast(
        `Create failed: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal open={open.value}
           onClose={() => { open.value = false }}
           title="New listing">
      {step.value === 1 && (
        <div class="sh-form sh-bazaar-create">
          <label>
            Title *
            <input value={title.value} maxLength={200}
              onInput={(e) => title.value = (e.target as HTMLInputElement).value} />
          </label>
          <label>
            Description
            <textarea value={description.value} rows={4} maxLength={2000}
              onInput={(e) => description.value = (e.target as HTMLTextAreaElement).value} />
          </label>

          <div>
            <strong style={{ fontSize: 'var(--sh-font-size-sm)' }}>Photos</strong>
            <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)', margin: '2px 0 8px' }}>
              Up to {MAX_IMAGES} images. Drag or pick from your device.
            </p>
            <div class="sh-bazaar-create-images">
              {imageUrls.value.map(entry => (
                <div key={entry.url} class="sh-bazaar-create-img">
                  <img src={entry.preview} alt="" />
                  <button type="button" class="sh-composer-remove-attach"
                          aria-label="Remove image"
                          onClick={() => removeImage(entry.url)}>✕</button>
                </div>
              ))}
              {imageUrls.value.length < MAX_IMAGES && (
                <button type="button" class="sh-bazaar-create-add"
                        onClick={() => fileRef.current?.click()}>
                  <span>＋</span>
                  <span>Add photo</span>
                </button>
              )}
              <input ref={fileRef} type="file" accept="image/*" multiple
                     style={{ display: 'none' }}
                     onChange={onFilesPicked} />
            </div>
            <UploadProgressBar />
          </div>

          <div class="sh-form-actions">
            <Button variant="secondary"
                    onClick={() => { open.value = false }}>
              Cancel
            </Button>
            <Button onClick={() => (step.value = 2)}
                    disabled={!title.value.trim()}>
              Next →
            </Button>
          </div>
        </div>
      )}
      {step.value === 2 && (
        <div class="sh-form sh-bazaar-create">
          <label>
            Mode
            <select value={mode.value}
                    onChange={(e) =>
                      mode.value = (e.target as HTMLSelectElement).value as BazaarMode}>
              <option value="fixed">Fixed price</option>
              <option value="offer">Accept offers</option>
              <option value="auction">Auction</option>
              <option value="bid_from">Bid from (starting price)</option>
              <option value="negotiable">Negotiable</option>
            </select>
          </label>

          <label>
            Currency
            <select value={currency.value}
                    onChange={(e) =>
                      currency.value = (e.target as HTMLSelectElement).value}>
              {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>

          {(mode.value === 'fixed' || mode.value === 'negotiable') && (
            <label>
              Price
              <input type="number" step="0.01" min="0" value={price.value}
                onInput={(e) => price.value = (e.target as HTMLInputElement).value} />
            </label>
          )}

          {(mode.value === 'auction' || mode.value === 'bid_from') && (
            <>
              <label>
                Starting price
                <input type="number" step="0.01" min="0" value={startPrice.value}
                  onInput={(e) => startPrice.value = (e.target as HTMLInputElement).value} />
              </label>
              <label>
                Bid increment (optional)
                <input type="number" step="0.01" min="0" value={stepPrice.value}
                  placeholder="e.g. 1.00"
                  onInput={(e) => stepPrice.value = (e.target as HTMLInputElement).value} />
              </label>
            </>
          )}

          <label>
            Duration
            <select value={String(durationDays.value)}
                    onChange={(e) =>
                      durationDays.value = parseInt(
                        (e.target as HTMLSelectElement).value,
                      ) || 7}>
              <option value="1">1 day</option>
              <option value="3">3 days</option>
              <option value="5">5 days</option>
              <option value="7">7 days</option>
            </select>
          </label>

          <div class="sh-form-actions">
            <Button variant="secondary" onClick={() => (step.value = 1)}>
              ← Back
            </Button>
            <Button onClick={submit} loading={submitting.value}>
              Create listing
            </Button>
          </div>
        </div>
      )}
    </Modal>
  )
}
