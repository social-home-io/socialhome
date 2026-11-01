/**
 * ``/apps/:appId/draw/:sessionId`` — the gift draw's result sheet on its
 * own address, where "Your gift draw is ready" leads (v_78). The same
 * Social Home sheet ``draw.reveal`` opens over an app; Done goes to Apps.
 */
import { useLocation, useRoute } from 'preact-iso'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { DrawResultSheet } from './DrawResultSheet'

export default function DrawResultPage() {
  const { params } = useRoute()
  const { route } = useLocation()
  useTitle(t('apps.draw.result.title_plain'))
  const appId = decodeURIComponent(params.appId ?? '')
  const sessionId = decodeURIComponent(params.sessionId ?? '')
  return (
    <div class="sh-page">
      <DrawResultSheet appId={appId} sessionId={sessionId} onClose={() => route('/apps')} />
    </div>
  )
}
