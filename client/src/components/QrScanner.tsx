/**
 * QrScanner — camera + image-upload QR decoding (one panel, both
 * methods inside).
 *
 * Promoted out of PairingFlow so the space-invite "join by code" card
 * can scan QRs too. Self-contained: owns its own ``scanError`` state
 * so it doesn't depend on PairingFlow's module-level signal. Callers
 * route errors through the optional ``onError`` callback if they need
 * toast / analytics; the panel always renders the error inline.
 *
 * Translation keys still live under ``pairing.*`` — the strings inside
 * (``"Couldn’t access the camera"`` etc.) are generic QR-scanner copy
 * that works for both pairing and invites. Migrate the keys to a
 * dedicated ``qr.*`` namespace when a third caller arrives; one shared
 * namespace is fine for two.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { Button } from './Button'
import { Spinner } from './Spinner'
import { t } from '@/i18n/i18n'

type BarcodeDetectorLike = {
  detect: (source: CanvasImageSource | ImageBitmap | Blob)
    => Promise<Array<{ rawValue: string }>>
}

export function barcodeDetectorSupported(): boolean {
  return typeof (window as unknown as { BarcodeDetector?: unknown })
    .BarcodeDetector === 'function'
}

function createDetector(): BarcodeDetectorLike | null {
  const Ctor = (window as unknown as {
    BarcodeDetector?: new (opts: { formats: string[] }) => BarcodeDetectorLike
  }).BarcodeDetector
  if (!Ctor) return null
  try {
    return new Ctor({ formats: ['qr_code'] })
  } catch {
    return null
  }
}

/**
 * Camera preview + continuous QR decode loop. Calls ``onPayload`` once
 * with the decoded string. Stream + detection loop tear down cleanly
 * on unmount.
 *
 * Camera errors (permission denied, no camera, no detector) are this
 * card's own: it renders them in place and nobody else repeats them.
 * The camera starts once per mount — the parent re-renders on every
 * state change and hands down fresh closures, so the effect reads the
 * latest ``onPayload`` through a ref instead of listing it as a dep.
 * Re-running the effect would restart (and re-fail) the camera on
 * every keystroke elsewhere in the panel.
 */
function QrCameraScanner({
  onPayload,
}: {
  onPayload: (raw: string) => void
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [starting, setStarting] = useState(true)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const onPayloadRef = useRef(onPayload)
  onPayloadRef.current = onPayload

  useEffect(() => {
    let cancelled = false
    let rafId: number | null = null
    let stream: MediaStream | null = null
    const detector = createDetector()
    if (!detector) {
      setErrorMsg(t('pairing.scan_no_detector'))
      setStarting(false)
      return
    }
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: 'environment' } },
        })
        if (cancelled || !videoRef.current) {
          stream.getTracks().forEach(t => t.stop())
          return
        }
        videoRef.current.srcObject = stream
        await videoRef.current.play().catch(() => null)
        setStarting(false)
        const loop = async () => {
          if (cancelled || !videoRef.current) return
          try {
            const results = await detector.detect(videoRef.current)
            const match = results.find(r => !!r.rawValue)
            if (match) {
              cancelled = true
              onPayloadRef.current(match.rawValue)
              return
            }
          } catch {
            // keep trying — detector throws on empty frames sometimes
          }
          rafId = requestAnimationFrame(() => { void loop() })
        }
        void loop()
      } catch (err: unknown) {
        const name = (err as { name?: string }).name ?? ''
        let msg: string
        if (name === 'NotAllowedError') {
          msg = t('pairing.scan_permission_denied')
        } else if (name === 'NotFoundError') {
          msg = t('pairing.scan_no_camera')
        } else {
          msg = t('pairing.scan_failed')
        }
        setErrorMsg(msg)
        setStarting(false)
      }
    })()
    return () => {
      cancelled = true
      if (rafId !== null) cancelAnimationFrame(rafId)
      if (stream) stream.getTracks().forEach(t => t.stop())
    }
  }, [])

  if (errorMsg) {
    return (
      <div class="sh-scan-error" role="alert">
        <div aria-hidden="true" class="sh-scan-error-icon">📷</div>
        <p>{errorMsg}</p>
      </div>
    )
  }
  return (
    <div class="sh-scan-camera">
      <video ref={videoRef} playsInline muted class="sh-scan-video" />
      <div class="sh-scan-frame" aria-hidden="true" />
      {starting && (
        <div class="sh-scan-starting">
          <Spinner />
          <span>{t('pairing.scan_starting')}</span>
        </div>
      )}
    </div>
  )
}

/**
 * Decode a QR from an uploaded image file. Returns the raw decoded
 * string or ``null`` if no QR was found / BarcodeDetector isn't
 * supported.
 */
export async function decodeImage(file: File): Promise<string | null> {
  if (!barcodeDetectorSupported()) return null
  const detector = createDetector()
  if (!detector) return null
  const bitmap = await createImageBitmap(file)
  try {
    const results = await detector.detect(bitmap)
    const match = results.find(r => !!r.rawValue)
    return match?.rawValue ?? null
  } finally {
    bitmap.close?.()
  }
}

export interface QrScannerProps {
  /** Called once when the panel decodes a QR (camera or upload). */
  onPayload: (raw: string) => void
  /**
   * Called when an uploaded image fails to decode (unreadable file, no
   * QR in it). Optional — the panel always renders the error inline;
   * this callback is only for parent-side toast / analytics. Camera
   * errors (permission denial, no camera, no detector support) stay on
   * the camera card and are NOT reported here, so a parent that mirrors
   * ``onError`` into its own line never shows them twice.
   */
  onError?: (msg: string) => void
  /**
   * Called when the user clicks "Cancel". Optional — if omitted, no
   * cancel button renders.
   */
  onCancel?: () => void
}

/**
 * Two-method QR scanner: live camera (when ``BarcodeDetector`` is
 * available) plus an "upload an image" fallback that works on every
 * browser the file picker reaches.
 */
export function QrScanner({ onPayload, onError, onCancel }: QrScannerProps) {
  const [decoding, setDecoding] = useState(false)
  // Upload / decode verdict only — camera errors render on the camera
  // card, so a camera failure can never overwrite a more recent upload
  // error here.
  const [scanError, setScanError] = useState<string | null>(null)

  const reportError = (msg: string) => {
    setScanError(msg)
    onError?.(msg)
  }

  const handleFile = async (ev: Event) => {
    const input = ev.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    setDecoding(true)
    setScanError(null)
    try {
      const raw = await decodeImage(file)
      if (!raw) {
        reportError(t('pairing.scan_no_code_in_image'))
        return
      }
      onPayload(raw)
    } catch {
      reportError(t('pairing.scan_decode_failed'))
    } finally {
      setDecoding(false)
      input.value = ''
    }
  }

  return (
    <div class="sh-scan-options">
      {barcodeDetectorSupported() && (
        <QrCameraScanner onPayload={onPayload} />
      )}
      {!barcodeDetectorSupported() && (
        <div class="sh-scan-no-camera">
          <p class="sh-muted">{t('pairing.scan_no_camera_hint')}</p>
        </div>
      )}
      {scanError && (
        <p class="sh-scan-error-inline" role="alert">{scanError}</p>
      )}
      <label class="sh-link sh-scan-upload-label">
        <input
          type="file"
          accept="image/*"
          class="sr-only"
          onChange={handleFile}
          disabled={decoding}
        />
        {t('pairing.scan_upload')}
      </label>
      {decoding && <Spinner />}
      {onCancel && (
        <div class="sh-pairing-actions">
          <Button variant="secondary" onClick={onCancel}>
            {t('common.cancel')}
          </Button>
        </div>
      )}
    </div>
  )
}
