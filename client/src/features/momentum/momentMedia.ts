/**
 * momentMedia — check and upload the one photo or video a moment (or a
 * reply) carries. Shared by the composer and the inline reply field on the
 * moment page, so both refuse the same things the same way: only pictures
 * and videos, a video no longer than 15 s (the server's limit too), an
 * unreadable video said as such, and upload errors described for the file.
 */
import { showToast } from '@/components/Toast'
import { uploadWithProgress } from '@/components/UploadProgress'
import { t } from '@/i18n/i18n'
import { describeUploadError } from '@/utils/uploadErrors'
import { formatSeconds, readVideoDurationMs } from '@/utils/videoDuration'

/** Longest video a moment takes (``MOMENT_MAX_VIDEO_MS`` on the server). */
export const MOMENT_MAX_VIDEO_MS = 15_000

export interface MomentMedia {
  /** The stored path that goes into ``media_url``. */
  url: string
  /** A signed URL the preview can load. */
  previewUrl: string
  type: 'image' | 'video'
  /** Videos only. */
  durationMs: number | null
}

/** Check and upload *file*; ``null`` (after a toast saying why) when it
 *  can't go with a moment. */
export async function prepareMomentMedia(file: File): Promise<MomentMedia | null> {
  const isVideo = file.type.startsWith('video/')
  const isImage = file.type.startsWith('image/')
  if (!isVideo && !isImage) {
    showToast(t('highlight.quick.pick_media'), 'error')
    return null
  }
  let durationMs: number | null = null
  if (isVideo) {
    const dur = await readVideoDurationMs(file)
    if (dur === null) {
      showToast(t('moment.composer.video_unreadable'), 'error')
      return null
    }
    if (dur > MOMENT_MAX_VIDEO_MS + 50) {
      showToast(
        t('moment.composer.video_too_long', {
          max: String(MOMENT_MAX_VIDEO_MS / 1000), len: formatSeconds(dur),
        }),
        'error',
      )
      return null
    }
    durationMs = Math.round(dur)
  }
  try {
    const result = await uploadWithProgress(file)
    return {
      url: result.url,
      previewUrl: result.signed_url ?? result.url,
      type: isVideo ? 'video' : 'image',
      durationMs,
    }
  } catch (err: unknown) {
    showToast(describeUploadError(err, { file }), 'error')
    return null
  }
}
