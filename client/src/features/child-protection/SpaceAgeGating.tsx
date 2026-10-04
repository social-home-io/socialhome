/**
 * SpaceAgeGating — space-level age restriction + discovery category (spec §23.105, §23.50).
 *
 * Household admins set a minimum age on a space: minors whose ``declared_age``
 * is below ``min_age`` are blocked from joining (backend enforcement in
 * ``SpaceService.add_member`` via ``ChildProtectionService.check_space_age_gate``).
 * The minimum age is saved via the cp age-gate endpoint (``min_age`` only).
 *
 * Separately, the **category** is a discovery topic for the space (how it
 * appears in public/global discovery). It is a plain Space attribute saved via
 * ``PATCH /api/spaces/{id}``.
 *
 * Listens for ``cp.age_gate_changed`` so a min-age change on another session
 * refreshes this one live. Category has no WS event (no live-refresh needed).
 */
import { useEffect } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { SPACE_CATEGORIES } from '@/components/spaceModeOptions'
import { t } from '@/i18n/i18n'

const VALID_MIN_AGES: number[] = [0, 13, 16, 18]

export function SpaceAgeGating({ spaceId }: { spaceId: string }) {
  // ``useSignal`` so writes from ``load()`` and ``save()`` land in the
  // same signal instance the JSX is subscribed to across re-renders.
  const minAge = useSignal(0)
  const category = useSignal('general')
  const saving = useSignal(false)
  const loaded = useSignal(false)

  const loadAgeGate = async () => {
    try {
      const data = await api.get(
        `/api/cp/spaces/${spaceId}/age-gate`,
      ) as { min_age?: number }
      minAge.value = Number(data.min_age) || 0
    } catch {
      /* unknown space or unauthenticated — leave default */
    }
  }

  const loadCategory = async () => {
    try {
      const data = await api.get(
        `/api/spaces/${spaceId}`,
      ) as { category?: string }
      category.value = data.category || 'general'
    } catch {
      /* unknown space or unauthenticated — leave default */
    }
  }

  const load = async () => {
    await Promise.all([loadAgeGate(), loadCategory()])
    loaded.value = true
  }

  useEffect(() => {
    void load()
    const off = ws.on('cp.age_gate_changed', (evt) => {
      const d = evt.data as { space_id?: string }
      if (d.space_id === spaceId) void loadAgeGate()
    })
    return () => { off() }
  }, [spaceId])

  const save = async () => {
    saving.value = true
    try {
      await Promise.all([
        api.patch(`/api/spaces/${spaceId}`, { category: category.value }),
        api.patch(`/api/cp/spaces/${spaceId}/age-gate`, { min_age: minAge.value }),
      ])
      showToast(t('space.age.saved'), 'success')
    } catch (e: unknown) {
      showToast((e as Error).message || t('space.age.save_failed'), 'error')
    } finally {
      saving.value = false
    }
  }

  if (!loaded.value) return null

  return (
    <div class="sh-age-gating sh-card">
      <h4>{t('space.settings_page.tab_age')}</h4>
      <p class="sh-muted">
        {t('space.age.intro')}
      </p>
      <div class="sh-form-row">
        <label>
          {t('space.age.min_age')}
          <select
            value={String(minAge.value)}
            onChange={(e) => minAge.value = Number((e.target as HTMLSelectElement).value)}>
            {VALID_MIN_AGES.map(a => (
              <option key={a} value={String(a)}>
                {a === 0 ? t('space.age.no_restriction') : `${a}+`}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('space.age.category')}
          <select
            value={category.value}
            onChange={(e) => category.value = (e.target as HTMLSelectElement).value}>
            {SPACE_CATEGORIES.map(c => (
              <option key={c.value} value={c.value}>{c.label}</option>
            ))}
          </select>
        </label>
        <Button onClick={save} loading={saving.value}>{t('common.save')}</Button>
      </div>
    </div>
  )
}
