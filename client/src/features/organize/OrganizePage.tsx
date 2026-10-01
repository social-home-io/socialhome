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
 * right tab. The summary count chips on each tab pull from the
 * existing ``tasks`` / ``shopping`` / ``stickies`` stores so a member
 * can see "3 to do · 4 in cart · 2 stickies" at a glance before
 * picking a tab.
 */
import { useEffect } from 'preact/hooks'
import { signal, useComputed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { TabHeader } from '@/components/TabHeader'
import { items as shoppingItems, ensureShopping } from '@/store/shopping'
import { pendingDeletes } from '@/utils/undoableDelete'
import { stickies } from '@/store/stickies'
import { tasks } from '@/store/tasks'
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

  // Live count chips — pull straight from the per-feature stores so
  // the labels track WS-driven updates without a refetch round-trip.
  // ``ensureShopping`` fetches only when nothing is loaded yet and
  // shares the in-flight request with the Shopping tab when both
  // mount together — one fetch, not two. A failure here is silent:
  // the chip just shows no count, and the tab shows its own error.
  useEffect(() => {
    ensureShopping().catch(() => { /* the Shopping tab reports it */ })
  }, [])

  useEffect(() => {
    activeTab.value = tabFromUrl(loc.url)
  }, [loc.url])

  const labels = useComputed<Readonly<Record<OrganizeTab, string>>>(() => {
    const todo = tasks.value.filter(t => t.status !== 'done').length
    const hidden = pendingDeletes.value
    const inCart = shoppingItems.value
      .filter(i => !i.completed && !hidden.has(i.id)).length
    const stuck = stickies.value.length
    return {
      tasks:    todo > 0    ? `Tasks · ${todo}`    : 'Tasks',
      shopping: inCart > 0  ? `Shopping · ${inCart}` : 'Shopping',
      stickies: stuck > 0   ? `Stickies · ${stuck}` : 'Stickies',
    }
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
        ariaLabel="Organize sections"
        onSelectTab={onSelectTab}
      />
      {activeTab.value === 'tasks'    && <TaskPage />}
      {activeTab.value === 'shopping' && <ShoppingPage />}
      {activeTab.value === 'stickies' && <StickyBoardPage />}
    </div>
  )
}
