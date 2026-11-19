/**
 * OwnHouseholdId — this household's own ID on the Connections page, for
 * everyone at home.
 *
 * Another household can only check "is it really them?" when the people
 * here can read their ID out: before the owner of a space makes someone
 * here an admin (:mod:`AdminGrantCheck` shows the owner this ID), or when
 * someone pairs through a shared connection and is asked for it.
 */
import { HouseholdId } from '@/components/HouseholdId'
import { instanceConfig } from '@/store/instance'
import { t } from '@/i18n/i18n'
import { SectionHeader } from './SectionHeader'

export function OwnHouseholdId() {
  const id = instanceConfig.value?.instance_id
  if (!id) return null
  return (
    <section class="sh-conn-sect" aria-labelledby="sh-conn-own-id-title">
      <SectionHeader id="sh-conn-own-id-title" title={t('connections.own_id.title')}
                     desc={t('connections.own_id.desc')} />
      <div class="sh-conn-own-id">
        <HouseholdId id={id} />
      </div>
    </section>
  )
}
