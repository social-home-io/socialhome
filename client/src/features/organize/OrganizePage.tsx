/**
 * OrganizePage — single hub for the household's organisational
 * surfaces (Tasks · Shopping · Stickies).
 *
 * Each of those three was its own routed page with its own sidebar
 * entry; individually low-traffic but collectively crowding the
 * AT-HOME group. Bundling them as tabs frees three sidebar slots and
 * stops them from competing for vertical real-estate with Feed /
 * Calendar (the daily-use surfaces).
 *
 * Tab state is URL-driven via ``?tab=`` so deep links from the corner
 * dashboard / quick-action chips / push notifications open on the
 * right tab. The count chips ("Tasks · 3", "Shopping · 4",
 * "Stickies · 2") read the stores, so they track WS updates:
 *
 * - tasks: open tasks across EVERY household list (``ensureAll``), not
 *   just the list the Tasks tab has open;
 * - shopping: unbought items, minus rows hidden behind an Undo toast;
 * - stickies: the household store's notes (minus pending deletes) —
 *   space boards have stores of their own.
 *
 * Every load is deduped with the tab that needs the same data, so a
 * deep link fetches each source once.
 */
import { useEffect } from 'preact/hooks'
import { signal, useComputed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { TabHeader } from '@/components/TabHeader'
import { items as shoppingItems, ensureShopping } from '@/store/shopping'
import { pendingDeletes } from '@/utils/undoableDelete'
import { householdStickyCount, householdStickyStore } from '@/store/stickies'
import { householdTaskStore } from '@/store/tasks'
import { t } from '@/i18n/i18n'
import TaskPage from '@/features/tasks/TaskPage'
import ShoppingPage from '@/features/shopping/ShoppingPage'
import StickyBoardPage from '@/features/stickies/StickyBoardPage'

type OrganizeTab = 'tasks' | 'shopping' | 'stickies'

const TABS: readonly OrganizeTab[] = ['tasks', 'shopping', 'stickies'] as const

const activeTab = signal<OrganizeTab>('tasks')


function tabFromUrl(url: string): OrganizeTab {
  const q = url.split('?')[1] ?? ''
  const t = new URLSearchParams(q).get('tab')
  if (t === 'shopping' || t === 'stickies') return t
  return 'tasks'
}


export default function OrganizePage() {
  const loc = useLocation()

  // Count chips. Each ``ensure*`` loads only what isn't loaded yet and
  // shares an in-flight request with the tab mounting alongside. A
  // failure is silent here: the chip shows no count, the tab its error.
  useEffect(() => {
    ensureShopping().catch(() => { /* the Shopping tab reports it */ })
    householdTaskStore.ensureAll().catch(() => { /* the Tasks tab reports it */ })
    householdStickyStore.ensure().catch(() => { /* the Stickies tab reports it */ })
  }, [])

  useEffect(() => {
    activeTab.value = tabFromUrl(loc.url)
  }, [loc.url])

  const labels = useComputed<Readonly<Record<OrganizeTab, string>>>(() => {
    const hidden = pendingDeletes.value
    const counts: Record<OrganizeTab, number> = {
      tasks: householdTaskStore.openCount.value,
      shopping: shoppingItems.value.filter(i => !i.completed && !hidden.has(i.id)).length,
      stickies: householdStickyCount.value ?? 0,
    }
    const label = (tab: OrganizeTab) => {
      // Keys for i18n:check: t('organize.tab.tasks') t('organize.tab.shopping') t('organize.tab.stickies')
      const name = t(`organize.tab.${tab}`)
      return counts[tab] > 0
        ? t('organize.tab_count', { name, n: String(counts[tab]) })
        : name
    }
    return { tasks: label('tasks'), shopping: label('shopping'), stickies: label('stickies') }
  })

  // Each child page owns its own ``useTitle`` ('Tasks' / list name,
  // 'Shopping', 'Sticky notes'); since the active child mounts last,
  // its title wins. No host-level ``useTitle`` needed.

  const onSelectTab = (tab: OrganizeTab) => {
    activeTab.value = tab
    // ``tasks`` is the default — no ?tab=tasks query param; otherwise
    // attach the tab so a copy-paste of the URL lands on the same
    // place the originator was looking at.
    const next = tab === 'tasks' ? '/organize' : `/organize?tab=${tab}`
    if (loc.url !== next) loc.route(next, true)
  }

  return (
    <div class="sh-organize-host">
      <TabHeader<OrganizeTab>
        activeTab={activeTab.value}
        visibleTabs={TABS}
        labels={labels.value}
        ariaLabel={t('organize.label')}
        onSelectTab={onSelectTab}
      />
      {activeTab.value === 'tasks'    && <TaskPage />}
      {activeTab.value === 'shopping' && <ShoppingPage />}
      {activeTab.value === 'stickies' && <StickyBoardPage />}
    </div>
  )
}
