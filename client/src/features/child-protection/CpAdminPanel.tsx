/**
 * CpAdminPanel — Child Protection admin panel (spec §23.103).
 *
 * Admin-only surface that lets the household set protection on / off per
 * user, supply a ``declared_age`` + optional DOB, and manage the list of
 * guardians for each protected minor.
 */
import { useEffect } from 'preact/hooks'
import { signal, useSignal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import type { User } from '@/types'
import { t } from '@/i18n/i18n'

interface MinorFormState {
  username: string
  declared_age: number
  date_of_birth: string
}

interface ProtStatus {
  is_minor: boolean
  declared_age: number
}

const users = signal<User[]>([])
/** Protection status keyed by user_id, from the admin-only
 *  ``/api/cp/protection`` endpoint. ``is_minor`` / ``declared_age`` are
 *  SENSITIVE_FIELDS stripped from ``/api/users``, so this is their only
 *  source — without it the "Protected" column can't reflect reality. */
const protection = signal<Record<string, ProtStatus>>({})
const loading = signal(true)
/** For the active row: pending enable form (username → state). */
const pendingEnable = signal<MinorFormState | null>(null)
const pendingDisable = signal<string | null>(null)
/** Guardian admin state for the currently expanded minor. */
const guardiansFor = signal<string | null>(null)
const guardians = signal<string[]>([])

async function load() {
  loading.value = true
  try {
    const [allUsers, prot] = await Promise.all([
      api.get('/api/users') as Promise<User[]>,
      api.get('/api/cp/protection') as Promise<{ users: Array<ProtStatus & { user_id: string }> }>,
    ])
    users.value = allUsers
    protection.value = Object.fromEntries(
      prot.users.map(p => [p.user_id, { is_minor: p.is_minor, declared_age: p.declared_age }]),
    )
  } catch (e: unknown) {
    showToast((e as Error).message || t('cp.load_failed'), 'error')
  } finally {
    loading.value = false
  }
}

async function loadGuardians(minorUserId: string) {
  try {
    const data = await api.get(`/api/cp/users/${minorUserId}/guardians`) as {
      guardians: string[]
    }
    guardians.value = data.guardians
  } catch {
    guardians.value = []
  }
}

async function enableProtection(form: MinorFormState) {
  try {
    const body: Record<string, unknown> = {
      enabled: true, declared_age: form.declared_age,
    }
    if (form.date_of_birth) body.date_of_birth = form.date_of_birth
    await api.post(
      `/api/cp/users/${form.username}/protection`, body,
    )
    showToast(t('cp.enabled'), 'success')
    pendingEnable.value = null
    void load()
  } catch (e: unknown) {
    showToast((e as Error).message || t('cp.enable_failed'), 'error')
  }
}

async function disableProtection(username: string) {
  try {
    await api.post(
      `/api/cp/users/${username}/protection`, { enabled: false },
    )
    showToast(t('cp.removed'), 'info')
    void load()
  } catch (e: unknown) {
    showToast((e as Error).message || t('cp.remove_failed'), 'error')
  }
  pendingDisable.value = null
}

async function addGuardian(minorUserId: string, guardianUserId: string) {
  try {
    await api.post(
      `/api/cp/users/${minorUserId}/guardians/${guardianUserId}`, {},
    )
    await loadGuardians(minorUserId)
  } catch (e: unknown) {
    showToast((e as Error).message || t('cp.guardian_add_failed'), 'error')
  }
}

async function removeGuardian(minorUserId: string, guardianUserId: string) {
  try {
    await api.delete(`/api/cp/users/${minorUserId}/guardians/${guardianUserId}`)
    await loadGuardians(minorUserId)
  } catch (e: unknown) {
    showToast((e as Error).message || t('cp.guardian_remove_failed'), 'error')
  }
}

export default function CpAdminPanel() {
  useEffect(() => {
    void load()
    // Reload when another admin toggles protection elsewhere.
    const off = ws.on('cp.protection_enabled', () => { void load() })
    const off2 = ws.on('cp.protection_disabled', () => { void load() })
    return () => { off(); off2() }
  }, [])

  if (loading.value) return <Spinner />

  return (
    <section class="sh-cp-admin sh-admin-section">
      <h2>{t('admin.tab.child_protection')}</h2>
      <p class="sh-muted">{t('cp.intro')}</p>

      <table class="sh-admin-table">
        <thead>
          <tr>
            <th>{t('admin.members.name')}</th><th>{t('admin.members.username')}</th>
            <th>{t('cp.col.protected')}</th><th>{t('admin.members.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {users.value.map(u => {
            const status = protection.value[u.user_id]
            const protectedUser = Boolean(status?.is_minor)
            return (
              <>
                <tr key={u.username}>
                  <td>{u.display_name}</td>
                  <td>@{u.username}</td>
                  <td>
                    {protectedUser
                      ? `🔒 ${t('cp.protected_age', { age: String(status!.declared_age) })}`
                      : '—'}
                  </td>
                  <td>
                    {protectedUser ? (
                      <>
                        <Button variant="secondary"
                                onClick={() => {
                                  guardiansFor.value =
                                    guardiansFor.value === u.user_id ? null : u.user_id
                                  if (guardiansFor.value) void loadGuardians(u.user_id)
                                }}>
                          {t('cp.guardians')}
                        </Button>
                        <Button variant="secondary"
                                onClick={() => pendingDisable.value = u.username}>
                          {t('cp.remove')}
                        </Button>
                      </>
                    ) : (
                      <Button
                        variant="secondary"
                        onClick={() => pendingEnable.value = {
                          username: u.username,
                          declared_age: 12,
                          date_of_birth: '',
                        }}>
                        {t('cp.mark_minor')}
                      </Button>
                    )}
                  </td>
                </tr>
                {guardiansFor.value === u.user_id && (
                  <tr><td colspan={4} class="sh-cp-guardians-row">
                    <GuardianList minorUserId={u.user_id} allUsers={users.value} />
                  </td></tr>
                )}
              </>
            )
          })}
        </tbody>
      </table>

      {pendingEnable.value && (
        <EnableForm
          state={pendingEnable.value}
          onChange={(s) => pendingEnable.value = s}
          onSubmit={() => enableProtection(pendingEnable.value!)}
          onCancel={() => pendingEnable.value = null}
        />
      )}

      <ConfirmDialog
        open={pendingDisable.value !== null}
        title={t('cp.remove_title')}
        message={t('cp.remove_body')}
        onConfirm={() => pendingDisable.value && disableProtection(pendingDisable.value)}
        onCancel={() => pendingDisable.value = null}
      />
    </section>
  )
}

/** Whole years from an ISO ``YYYY-MM-DD`` birth date to today, or null if
 *  unparseable. Used to auto-fill the declared age from a birthday. */
export function ageFromDob(dob: string): number | null {
  const d = new Date(dob)
  if (Number.isNaN(d.getTime())) return null
  const today = new Date()
  let age = today.getFullYear() - d.getFullYear()
  const m = today.getMonth() - d.getMonth()
  if (m < 0 || (m === 0 && today.getDate() < d.getDate())) age -= 1
  return age
}

function EnableForm({ state, onChange, onSubmit, onCancel }: {
  state: MinorFormState
  onChange: (s: MinorFormState) => void
  onSubmit: () => void
  onCancel: () => void
}) {
  return (
    <div class="sh-cp-enable-form sh-card">
      <h3>{t('cp.enable_title', { username: state.username })}</h3>
      <label>
        {t('cp.declared_age')}
        <input type="number" min={0} max={17}
          value={state.declared_age}
          onInput={(e) => onChange({
            ...state,
            declared_age: Math.max(0, Math.min(17, Number(
              (e.target as HTMLInputElement).value,
            ) || 0)),
          })} />
      </label>
      <label>
        {t('cp.dob')}
        <input type="date"
          value={state.date_of_birth}
          onInput={(e) => {
            const dob = (e.target as HTMLInputElement).value
            // Auto-fill the age from the birthday (clamped to the field's
            // 0–17 minor range). The admin can still adjust it afterwards.
            const age = dob ? ageFromDob(dob) : null
            onChange({
              ...state,
              date_of_birth: dob,
              declared_age: age === null
                ? state.declared_age
                : Math.max(0, Math.min(17, age)),
            })
          }} />
      </label>
      <div class="sh-row">
        <Button onClick={onSubmit}>{t('cp.enable')}</Button>
        <Button variant="secondary" onClick={onCancel}>{t('common.cancel')}</Button>
      </div>
    </div>
  )
}

function GuardianList({ minorUserId, allUsers }: {
  minorUserId: string
  allUsers: User[]
}) {
  const candidates = allUsers.filter(u => u.user_id !== minorUserId)
  const picker = useSignal<string>('')

  return (
    <div class="sh-cp-guardian-list">
      <strong>{t('cp.guardians')}</strong>
      <ul>
        {guardians.value.length === 0 && <li class="sh-muted">{t('cp.no_guardians')}</li>}
        {guardians.value.map(gid => {
          const u = allUsers.find(x => x.user_id === gid)
          return (
            <li key={gid} class="sh-row">
              <span>{u ? u.display_name : gid}</span>
              <Button variant="secondary"
                      onClick={() => removeGuardian(minorUserId, gid)}>
                {t('cp.guardian_remove')}
              </Button>
            </li>
          )
        })}
      </ul>
      <div class="sh-row">
        <select value={picker.value}
                onChange={(e) => picker.value = (e.target as HTMLSelectElement).value}>
          <option value="">{t('cp.pick_guardian')}</option>
          {candidates.map(u => (
            <option key={u.user_id} value={u.user_id}>
              {u.display_name} (@{u.username})
            </option>
          ))}
        </select>
        <Button onClick={() => {
          if (picker.value) {
            void addGuardian(minorUserId, picker.value)
            picker.value = ''
          }
        }}>{t('cp.guardian_add')}</Button>
      </div>
    </div>
  )
}
