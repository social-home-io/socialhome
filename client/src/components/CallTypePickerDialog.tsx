/**
 * CallTypePickerDialog — "audio or video?" picker for outbound calls (§26.2).
 *
 * Mounted at the app root so any thread-header / call-back button can
 * surface the picker without owning its own dialog. Follows the
 * shared-Modal + signal-driven open pattern used by ``StickyDialog``,
 * ``CalendarEventDialog``, ``NewDmDialog``, etc., so the washi-tape
 * chrome / focus trap / Escape close are consistent across the app.
 *
 * Flow:
 *   1. ``openCallTypePicker(conversationId)`` flips the open signal.
 *   2. User picks Audio or Video — :func:`startCall` acquires media,
 *      creates the SDP offer, POSTs ``/api/calls`` with the chosen
 *      ``call_type`` and the dialog routes to the in-call page.
 *   3. The chosen ``call_type`` is fixed at offer time on the backend
 *      (spec §26.5); mid-call camera enable/disable is handled by
 *      :func:`InCallPage.toggleCamera`.
 */
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { startCall } from '@/features/calls/callSession'
import { showCallError } from '@/features/calls/CallEmbedBlockedDialog'
import { CallEmbedBlockedError } from '@/features/calls/embedPolicy'
import { Modal } from './Modal'
import { t } from '@/i18n/i18n'

const open = signal(false)
const conversationId = signal<string | null>(null)
const submitting = signal(false)

export function openCallTypePicker(convId: string): void {
  conversationId.value = convId
  submitting.value = false
  open.value = true
}

export function CallTypePickerDialog() {
  const loc = useLocation()

  const start = async (callType: 'audio' | 'video') => {
    const convId = conversationId.value
    if (!convId || submitting.value) return
    submitting.value = true
    try {
      // Acquires the mic/camera, creates the real SDP offer and posts it.
      const callId = await startCall(convId, callType)
      open.value = false
      loc.route(`/calls/${callId}`)
    } catch (err: unknown) {
      // An embed that denies the mic can't be retried from here — swap the
      // picker for the "open in its own tab" dialog.
      if (err instanceof CallEmbedBlockedError) open.value = false
      showCallError(t('calls.start_failed'), err)
      submitting.value = false
    }
  }

  if (!open.value) return null
  return (
    <Modal
      open={open.value}
      onClose={() => { open.value = false }}
      title={t('calls.picker.title')}
    >
      <div class="sh-call-picker" role="group" aria-label={t('calls.picker.aria')}>
        <button
          type="button"
          class="sh-call-picker-tile"
          onClick={() => void start('audio')}
          disabled={submitting.value}
          aria-label={t('calls.picker.audio_aria')}
        >
          <span class="sh-call-picker-icon" aria-hidden="true">📞</span>
          <span class="sh-call-picker-label">{t('calls.picker.audio')}</span>
          <span class="sh-call-picker-meta">{t('calls.picker.audio_hint')}</span>
        </button>
        <button
          type="button"
          class="sh-call-picker-tile"
          onClick={() => void start('video')}
          disabled={submitting.value}
          aria-label={t('calls.picker.video_aria')}
        >
          <span class="sh-call-picker-icon" aria-hidden="true">📹</span>
          <span class="sh-call-picker-label">{t('calls.picker.video')}</span>
          <span class="sh-call-picker-meta">{t('calls.picker.video_hint')}</span>
        </button>
      </div>
    </Modal>
  )
}
