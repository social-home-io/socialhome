import { useRoute } from 'preact-iso'
import { ConversationView } from './ConversationView'

// Re-exported so existing imports (and tests) keep working.
export { canEditMessage, columnReverseDistFromBottom, isAtLiveEdge } from './ConversationView'

/** ``/dms/:id`` — the routed DM / group-DM thread page: the full
 *  :func:`ConversationView` with its page chrome (back chevron, TopBar
 *  title, full-bleed layout). */
export default function DmThreadPage() {
  const { params } = useRoute()
  return <ConversationView conversationId={params.id} />
}
