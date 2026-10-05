/**
 * QrCodeImg — render a QR image from a string payload.
 *
 * Encodes ``data`` to a PNG data-URL via the ``qrcode`` library and
 * displays it as an ``<img>``. Uses error-correction level M (15%
 * redundancy) — plenty for a short URL and keeps the code visually
 * clean. While generating, shows a sized skeleton so the layout
 * doesn't jump on mount.
 */
import { useEffect, useState } from 'preact/hooks'
import QRCode from 'qrcode'
import { t } from '@/i18n/i18n'

export interface QrCodeImgProps {
  data: string
  size?: number
  alt?: string
}

export function QrCodeImg({
  data,
  size = 220,
  alt = 'QR code',
}: QrCodeImgProps) {
  const [src, setSrc] = useState<string | null>(null)
  useEffect(() => {
    let stopped = false
    QRCode.toDataURL(data, {
      errorCorrectionLevel: 'M',
      margin: 1,
      width: size * 2,   // 2× for retina
      color: { dark: '#0f172a', light: '#ffffff' },
    }).then(url => { if (!stopped) setSrc(url) })
      .catch(() => { /* leave src null */ })
    return () => { stopped = true }
  }, [data, size])
  if (!src) {
    return (
      <div class="sh-qr-skeleton"
           style={{ width: size, height: size }}
           aria-label={t('qr.generating', { name: alt })} />
    )
  }
  return (
    <img src={src} width={size} height={size}
         class="sh-qr-code" alt={alt} />
  )
}
