/**
 * PagesPage — the household Markdown wiki (§23.58 / §23.72): the shared
 * {@link PagesView} on the household scope (``/api/pages``, edit locks,
 * admins restore old versions).
 */
import { useTitle } from '@/store/pageTitle'
import { t } from '@/i18n/i18n'
import { PagesView } from './PagesView'
import { householdPageScope } from './scope'

export default function PagesPage() {
  useTitle(t('pages.title'))
  return <PagesView scope={householdPageScope()} />
}
