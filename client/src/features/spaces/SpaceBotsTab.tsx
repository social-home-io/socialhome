/**
 * SpaceBotsTab — "Bots & automations" panel inside SpaceSettingsPage.
 *
 * Two permission tiers:
 *   - Admins/owners see everything, can toggle `bot_enabled`, create
 *     shared (scope=space) and personal (scope=member) bots, and manage
 *     any bot in the space.
 *   - Members see every bot but can only create/manage their OWN
 *     member-scope bots. The shared household bots are read-only for them.
 *
 * The newly-issued Bearer token is the core UX concern. It's the ONLY
 * time the user gets to see it; losing it means they must rotate. The
 * reveal panel is deliberately loud (toast + copy button + warning) so a
 * user can't miss it.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws, type WsEvent } from '@/ws'
import { Button } from '@/components/Button'
import { BotAvatar } from '@/components/BotAvatar'
import { showToast } from '@/components/Toast'
import type { BotScope, SpaceBot, SpaceBotWithToken } from '@/types'
import { confirmDialog } from '@/components/confirm'
import { t } from '@/i18n/i18n'

interface SpaceBotsTabProps {
  spaceId: string
  /** True when the active user is owner or admin. Drives whether the
   *  `bot_enabled` toggle and the "Create shared bot" option are shown. */
  canAdmin: boolean
  currentUserId: string | null
  /** Whether the parent space has `bot_enabled = true`. The tab shows
   *  a prominent disabled-state banner when this is false so admins
   *  understand why bots can't post even if they're registered. */
  botEnabled: boolean
  onBotEnabledChange: (enabled: boolean) => void
}

export function SpaceBotsTab({
  spaceId,
  canAdmin,
  currentUserId,
  botEnabled,
  onBotEnabledChange,
}: SpaceBotsTabProps) {
  const [bots, setBots] = useState<SpaceBot[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [revealed, setRevealed] = useState<SpaceBotWithToken | null>(null)

  /** ``quiet`` is the live-refresh path: no spinner, and a failed
   *  refresh keeps the list on screen instead of blanking it. */
  const reload = async ({ quiet = false } = {}) => {
    if (!quiet) setLoading(true)
    try {
      setBots(await api.get(`/api/spaces/${spaceId}/bots`) as SpaceBot[])
    } catch (err: unknown) {
      if (quiet) return
      showToast(t('space.bots.load_failed', { error: (err as Error).message }), 'error')
      setBots([])
    } finally {
      if (!quiet) setLoading(false)
    }
  }

  useEffect(() => { void reload() }, [spaceId])

  // Live: another member / device created, renamed, deleted or rotated
  // a bot in this space. The frames are thin nudges (``bot`` is the
  // token-free public shape), so refetch the canonical list. A token
  // reveal on screen stays put — it lives in its own state.
  const reloadRef = useRef(reload)
  reloadRef.current = reload
  useEffect(() => {
    const onBotFrame = (e: WsEvent) => {
      if ((e.data as { space_id?: string }).space_id !== spaceId) return
      void reloadRef.current({ quiet: true })
    }
    const offs = [
      'space.bot.created', 'space.bot.updated',
      'space.bot.deleted', 'space.bot.token_rotated',
    ].map(type => ws.on(type, onBotFrame))
    return () => { offs.forEach(off => off()) }
  }, [spaceId])

  const spaceBots = bots.filter(b => b.scope === 'space')
  const memberBots = bots.filter(b => b.scope === 'member')

  const canManage = (bot: SpaceBot): boolean =>
    canAdmin || (bot.scope === 'member' && bot.created_by === currentUserId)

  return (
    <div class="sh-bots-tab">
      {/* Explainer + opt-in toggle. The toggle controls the space-wide
          kill-switch; admins only. When off, all POST /api/bot-bridge/*
          requests for this space return 403 even if bot tokens remain
          valid — so disabling is cheap, reversible, and non-destructive. */}
      <section class="sh-bots-tab__intro">
        <h2>{t('space.settings_page.tab_bots')}</h2>
        <p class="sh-muted">
          {t('space.bots.intro')}
        </p>
        {canAdmin ? (
          <label class="sh-bots-toggle">
            <input
              type="checkbox"
              checked={botEnabled}
              onChange={async (e) => {
                const next = (e.target as HTMLInputElement).checked
                try {
                  await api.patch(`/api/spaces/${spaceId}`, { bot_enabled: next })
                  onBotEnabledChange(next)
                  showToast(next ? t('space.bots.enabled') : t('space.bots.disabled'), 'success')
                } catch (err: unknown) {
                  showToast(t('space.bots.update_failed', { error: (err as Error).message }), 'error')
                }
              }}
            />
            <span>{t('space.bots.allow')}</span>
          </label>
        ) : !botEnabled && (
          <div class="sh-bots-disabled-banner">
            {t('space.bots.disabled_banner')}
          </div>
        )}
      </section>

      {/* Reveal panel — shown immediately after create / rotate. Tokens
          never come back from the API once dismissed, so we hold it in
          component state and force the user to explicitly acknowledge. */}
      {revealed && (
        <TokenReveal
          bot={revealed}
          onDismiss={() => setRevealed(null)}
        />
      )}

      {loading ? (
        <p class="sh-muted">{t('space.bots.loading')}</p>
      ) : (
        <>
          <BotSection
            title={t('space.bots.shared_title')}
            description={t('space.bots.shared_desc')}
            bots={spaceBots}
            canManage={canManage}
            spaceId={spaceId}
            onChanged={reload}
            onReveal={setRevealed}
            emptyMessage={
              canAdmin
                ? t('space.bots.shared_empty_admin')
                : t('space.bots.shared_empty')
            }
          />
          <BotSection
            title={t('space.bots.personal_title')}
            description={t('space.bots.personal_desc')}
            bots={memberBots}
            canManage={canManage}
            spaceId={spaceId}
            onChanged={reload}
            onReveal={setRevealed}
            emptyMessage={t('space.bots.personal_empty')}
          />

          <div class="sh-bots-create-panel">
            {creating ? (
              <CreateBotForm
                spaceId={spaceId}
                canAdmin={canAdmin}
                onCancel={() => setCreating(false)}
                onCreated={(bot) => {
                  setCreating(false)
                  setRevealed(bot)
                  void reload()
                }}
              />
            ) : (
              <Button onClick={() => setCreating(true)} disabled={!botEnabled}>
                + {t('space.bots.create')}
              </Button>
            )}
            {!botEnabled && (
              <p class="sh-muted" style={{ marginTop: 'var(--sh-space-xs)' }}>
                {t('space.bots.turn_on_first')}
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}

// ─── Subcomponents ─────────────────────────────────────────────────────

function BotSection({
  title,
  description,
  bots,
  canManage,
  spaceId,
  onChanged,
  onReveal,
  emptyMessage,
}: {
  title: string
  description: string
  bots: SpaceBot[]
  canManage: (b: SpaceBot) => boolean
  spaceId: string
  onChanged: () => void | Promise<void>
  onReveal: (b: SpaceBotWithToken) => void
  emptyMessage: string
}) {
  return (
    <section class="sh-bots-section">
      <h3>{title}</h3>
      <p class="sh-muted">{description}</p>
      {bots.length === 0 ? (
        <p class="sh-bots-empty">{emptyMessage}</p>
      ) : (
        <ul class="sh-bots-list">
          {bots.map(b => (
            <BotRow
              key={b.bot_id}
              bot={b}
              manage={canManage(b)}
              spaceId={spaceId}
              onChanged={onChanged}
              onReveal={onReveal}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

function BotRow({
  bot,
  manage,
  spaceId,
  onChanged,
  onReveal,
}: {
  bot: SpaceBot
  manage: boolean
  spaceId: string
  onChanged: () => void | Promise<void>
  onReveal: (b: SpaceBotWithToken) => void
}) {
  const [busy, setBusy] = useState(false)

  const rotate = async () => {
    if (!await confirmDialog(
      t('space.bots.rotate_confirm', { name: bot.name }), { destructive: true })) return
    setBusy(true)
    try {
      const result = await api.post(
        `/api/spaces/${spaceId}/bots/${bot.bot_id}/token`,
      ) as SpaceBotWithToken
      onReveal(result)
      void onChanged()
    } catch (err: unknown) {
      showToast(t('space.bots.rotate_failed', { error: (err as Error).message }), 'error')
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!await confirmDialog(
      t('space.bots.delete_confirm', { name: bot.name }), { destructive: true })) return
    setBusy(true)
    try {
      await api.delete(`/api/spaces/${spaceId}/bots/${bot.bot_id}`)
      showToast(t('space.bots.deleted', { name: bot.name }), 'success')
      void onChanged()
    } catch (err: unknown) {
      showToast(t('space.bots.delete_failed', { error: (err as Error).message }), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <li class="sh-bot-row">
      <BotAvatar
        bot={{
          bot_id: bot.bot_id,
          scope: bot.scope,
          name: bot.name,
          icon: bot.icon,
          created_by_display_name: bot.created_by,
        }}
        size={36}
      />
      <div class="sh-bot-row__meta">
        <div class="sh-bot-row__name">{bot.name}</div>
        <div class="sh-bot-row__slug">
          <code>{bot.slug}</code>
          <span class="sh-muted">·</span>
          <span class="sh-muted">
            {t('space.bots.use_service')} <code>notify.social_home_{bot.slug.replace(/-/g, '_')}</code>
          </span>
        </div>
      </div>
      {manage && (
        <div class="sh-bot-row__actions">
          <Button variant="secondary" onClick={rotate} disabled={busy}>
            {t('space.bots.rotate')}
          </Button>
          <Button variant="danger" onClick={remove} disabled={busy}>
            {t('common.delete')}
          </Button>
        </div>
      )}
    </li>
  )
}

function CreateBotForm({
  spaceId,
  canAdmin,
  onCancel,
  onCreated,
}: {
  spaceId: string
  canAdmin: boolean
  onCancel: () => void
  onCreated: (b: SpaceBotWithToken) => void
}) {
  const [scope, setScope] = useState<BotScope>(canAdmin ? 'space' : 'member')
  const [slug, setSlug] = useState('')
  const [name, setName] = useState('')
  const [icon, setIcon] = useState('🔔')
  const [saving, setSaving] = useState(false)

  // Auto-suggest a slug from the name the first time the user types. Once
  // they edit the slug directly we stop auto-generating so we don't fight
  // their intent. Tracked by comparing slug to the last auto-generated value.
  const [lastAuto, setLastAuto] = useState('')
  const handleNameChange = (v: string) => {
    setName(v)
    if (slug === '' || slug === lastAuto) {
      const auto = v.toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 32)
      setSlug(auto)
      setLastAuto(auto)
    }
  }

  const submit = async (e: Event) => {
    e.preventDefault()
    if (!slug || !name || !icon) return
    setSaving(true)
    try {
      const bot = await api.post(`/api/spaces/${spaceId}/bots`, {
        scope, slug, name, icon,
      }) as SpaceBotWithToken
      onCreated(bot)
    } catch (err: unknown) {
      showToast(t('space.bots.create_failed', { error: (err as Error).message }), 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form class="sh-bot-create" onSubmit={submit}>
      <h3>{t('space.bots.new_title')}</h3>
      {canAdmin && (
        <div class="sh-bot-create__row">
          <label>{t('space.bots.type')}</label>
          <div class="sh-bot-create__scope">
            <label>
              <input
                type="radio"
                name="scope"
                value="space"
                checked={scope === 'space'}
                onChange={() => setScope('space')}
              />
              <span>{t('space.bots.type_shared')}</span>
              <small class="sh-muted">
                {t('space.bots.type_shared_hint')}
              </small>
            </label>
            <label>
              <input
                type="radio"
                name="scope"
                value="member"
                checked={scope === 'member'}
                onChange={() => setScope('member')}
              />
              <span>{t('space.bots.type_personal')}</span>
              <small class="sh-muted">
                {t('space.bots.type_personal_hint')}
              </small>
            </label>
          </div>
        </div>
      )}
      <div class="sh-bot-create__row">
        <label>{t('space.bots.icon')}</label>
        <input
          type="text"
          value={icon}
          maxLength={8}
          onInput={(e) => setIcon((e.target as HTMLInputElement).value)}
          required
        />
      </div>
      <div class="sh-bot-create__row">
        <label>{t('space.bots.name')}</label>
        <input
          type="text"
          value={name}
          placeholder={t('space.bots.name_placeholder')}
          maxLength={48}
          onInput={(e) => handleNameChange((e.target as HTMLInputElement).value)}
          required
        />
      </div>
      <div class="sh-bot-create__row">
        <label>{t('space.bots.slug')}</label>
        <input
          type="text"
          value={slug}
          placeholder="doorbell"
          pattern="[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?"
          maxLength={32}
          onInput={(e) => setSlug((e.target as HTMLInputElement).value)}
          required
        />
        <small class="sh-muted">
          {t('space.bots.slug_hint')}
          {' '}(<code>notify.social_home_{slug.replace(/-/g, '_') || '…'}</code>).
        </small>
      </div>
      <div class="sh-bot-create__actions">
        <Button type="submit" disabled={saving}>
          {saving ? t('space.bots.creating') : t('space.bots.create_submit')}
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel}>
          {t('common.cancel')}
        </Button>
      </div>
    </form>
  )
}

function TokenReveal({
  bot,
  onDismiss,
}: {
  bot: SpaceBotWithToken
  onDismiss: () => void
}) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(bot.token)
      setCopied(true)
      showToast(t('space.bots.token_copied'), 'success')
    } catch {
      showToast(t('space.bots.copy_failed'), 'error')
    }
  }
  return (
    <div class="sh-bot-reveal" role="alertdialog" aria-live="assertive">
      <div class="sh-bot-reveal__title">
        🔐 {t('space.bots.reveal_title', { name: bot.name })}
      </div>
      <p>
        <strong>{t('space.bots.reveal_once')}</strong>{' '}
        {t('space.bots.reveal_body')}
      </p>
      <div class="sh-bot-reveal__token">
        <code>{bot.token}</code>
        <Button onClick={copy}>
          {copied ? t('space.bots.copied') : t('space.bots.copy')}
        </Button>
      </div>
      <details>
        <summary class="sh-muted">{t('space.bots.snippet')}</summary>
        <pre class="sh-bot-reveal__snippet"><code>{`# configuration.yaml
rest_command:
  sh_${bot.slug.replace(/-/g, '_')}:
    url: "http://<socialhome-host>/api/bot-bridge/spaces/${bot.space_id}"
    method: POST
    headers:
      authorization: "Bearer ${bot.token}"
      content-type: "application/json"
    payload: '{"title": "{{ title }}", "message": "{{ message }}"}'`}</code></pre>
      </details>
      <div class="sh-bot-reveal__actions">
        <Button onClick={onDismiss}>{t('space.bots.saved_token')}</Button>
      </div>
    </div>
  )
}
