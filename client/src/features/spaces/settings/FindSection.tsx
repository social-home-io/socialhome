/**
 * Settings › Who can find it — the space's tier (Private / Household /
 * Public / Global, the app's four tiers) and, for the owner, how it uses
 * the GFS.
 *
 * A tier change is never a silent save: it is a proposal (v_16) that
 * applies at once only when one admin decides alone. Picking a tier opens
 * a short confirm row that says how many admins must agree; moving into
 * Global from the owner household also names the GFSs it is listed on.
 *
 * The owner-only GFS switches below save the moment they flip, like the
 * rest of the page: the private-space GFS (turning it off asks first — it
 * ends the space's GFS links), the private channel's GFSs, and how members
 * post through the GFS (trusted / strict).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { ChipRadioGroup } from '@/components/ChipRadioGroup'
import { confirmDialog } from '@/components/confirm'
import {
  GfsChecklist, defaultGfsSelection, revealGfsChecklist, type GfsChoice,
} from '@/components/GfsChecklist'
import {
  PrivateChannelPicker, sameGfsSet, type PrivateChannelChoices,
} from '@/components/PrivateChannelPicker'
import { ProtectedNotice, isRestricted } from '@/components/ProtectedNotice'
import { RadioCardGroup } from '@/components/RadioCardGroup'
import { SpaceGfsListing } from '@/components/SpaceGfsListing'
import { visibilityOptions } from '@/components/spaceModeOptions'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import type { GfsConnection, GfsPublishMode, Space } from '@/types'
import {
  patchSpace, useServerValue, type LinkMemberHousehold,
} from './spacePatch'
import { SwitchRow } from './SettingsRows'

type Tier = Space['space_type']

/** "With 2 admins, a change needs both to agree." — the quorum line. */
export function quorumNote(admins: number): string {
  if (admins <= 1) return t('space.settings_page.find.quorum_one')
  if (admins === 2) return t('space.settings_page.find.quorum_two')
  return t('space.settings_page.find.quorum_many', {
    n: String(admins), needed: String(Math.floor(admins / 2) + 1),
  })
}

interface Props {
  space: Space
  isOwner: boolean
  isRemoteSpace: boolean
  /** Owners + admins, every household — the proposal electorate. */
  adminCount: number
  onUpdate: () => void
}

export function FindSection({ space, isOwner, isRemoteSpace, adminCount, onUpdate }: Props) {
  const [tier, setTier] = useServerValue<Tier>(space.space_type)
  const pending = tier !== space.space_type
  const tierChecklistRef = useRef<HTMLFieldSetElement | null>(null)
  const [gfsChoices, setGfsChoices] = useState<GfsChoice[]>([])
  const [gfsSelected, setGfsSelected] = useState<string[]>([])
  const [gfsError, setGfsError] = useState<string | null>(null)
  const [gfsLoadFailed, setGfsLoadFailed] = useState(false)
  const [busy, setBusy] = useState(false)

  // Only the owner household chooses GFSs, so a remote stub loads nothing.
  const loadGfs = () => {
    setGfsLoadFailed(false)
    void api.get<GfsConnection[]>('/api/gfs/connections')
      .then((rows) => {
        const active = rows
          .filter(g => g.status === 'active')
          .map(({ id, display_name, inbox_url }) => ({ id, display_name, inbox_url }))
        setGfsChoices(active)
        // Exactly one server: nothing to decide, so it starts ticked.
        setGfsSelected(defaultGfsSelection(active))
      })
      .catch(() => {
        setGfsChoices([])
        setGfsLoadFailed(true)
      })
  }
  useEffect(() => { if (!isRemoteSpace) loadGfs() }, [space.id, isRemoteSpace])

  const choosesGfs = tier === 'global' && space.space_type !== 'global' && !isRemoteSpace

  const propose = async () => {
    if (!pending) return
    if (choosesGfs && gfsSelected.length === 0) {
      setGfsError(t('error.gfs_selection_required'))
      revealGfsChecklist(tierChecklistRef)
      return
    }
    setBusy(true)
    try {
      const res = await api.post<{ proposal?: { status?: string } }>(
        `/api/spaces/${space.id}/proposals`,
        {
          action: 'set_public_tier',
          space_type: tier,
          ...(choosesGfs ? { gfs_ids: gfsSelected } : {}),
        },
      )
      if (res?.proposal?.status === 'executed') {
        showToast(t('space.tier.changed'), 'success')
        onUpdate()
      } else {
        showToast(t('space.tier.proposed'), 'info')
        setTier(space.space_type)
      }
    } catch (e: unknown) {
      showToast((e as Error)?.message || t('space.tier.failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  const tierName = visibilityOptions().find(o => o.value === tier)?.title ?? tier
  const options = visibilityOptions().map(o => ({
    ...o,
    disabled: (o.value === 'public' || o.value === 'global') && isRestricted('public_spaces'),
  }))

  return (
    <div class="sh-ss-card">
      <RadioCardGroup
        legend={t('space.settings_page.find.legend')}
        name={`space-tier-${space.id}`}
        value={tier}
        options={options}
        onChange={(v) => { setTier(v as Tier); setGfsError(null) }}
      />
      {isRestricted('public_spaces') && <ProtectedNotice capability="public_spaces" />}
      <p class="sh-ss-hint" data-testid="tier-quorum">{quorumNote(adminCount)}</p>

      {pending && (
        <div class="sh-ss-pending" data-testid="tier-pending">
          {choosesGfs && gfsChoices.length > 0 && (
            <GfsChecklist
              ref={tierChecklistRef}
              name="space-tier"
              choices={gfsChoices}
              selected={gfsSelected}
              onChange={(ids) => { setGfsSelected(ids); setGfsError(null) }}
              error={gfsError}
            />
          )}
          {choosesGfs && gfsLoadFailed && (
            <p class="sh-form-error" role="alert" data-testid="tier-gfs-load-failed">
              {t('space.gfs_listing.load_failed')}{' '}
              <Button variant="secondary" onClick={loadGfs}>{t('common.retry')}</Button>
            </p>
          )}
          {choosesGfs && !gfsLoadFailed && gfsChoices.length === 0 && (
            <p class="sh-muted" data-testid="tier-gfs-none">
              {currentUser.value?.is_admin === true ? (
                <>
                  {t('space.create.gfs_hint_before')}
                  <a href={addBase('/connections')}>{t('space.create.gfs_hint_link')}</a>
                  {t('space.create.gfs_hint_after')}
                </>
              ) : t('space.create.gfs_hint_member')}
            </p>
          )}
          {tier === 'global' && space.space_type !== 'global' && isRemoteSpace && (
            <p class="sh-muted" data-testid="tier-gfs-owner-chooses">
              {t('space.tier.owner_chooses_gfs')}
            </p>
          )}
          {space.space_type === 'global' && (
            <p class="sh-muted" data-testid="tier-leaving-global">
              {t('space.tier.leaving_global')}
            </p>
          )}
          <div class="sh-ss-pending__actions">
            <Button variant="ghost" onClick={() => { setTier(space.space_type); setGfsError(null) }}>
              {t('common.cancel')}
            </Button>
            <Button
              loading={busy}
              disabled={choosesGfs && gfsChoices.length === 0}
              onClick={() => void propose()}
            >
              {adminCount <= 1
                ? t('space.settings_page.find.apply', { tier: tierName })
                : t('space.settings_page.find.propose', { tier: tierName })}
            </Button>
          </div>
        </div>
      )}

      {/* While the owner household picks GFSs for a move into Global, the
       *  current tier's note would contradict the checklist — hide it. */}
      {!choosesGfs && (
        <SpaceGfsListing space={space} isOwner={isOwner} isRemoteSpace={isRemoteSpace} />
      )}

      {isOwner && space.space_type === 'private' && (
        <PrivateGfs space={space} isRemoteSpace={isRemoteSpace} onUpdate={onUpdate} />
      )}
      {isOwner && <PublishMode space={space} onUpdate={onUpdate} />}
    </div>
  )
}

/** Whether a PRIVATE space uses the GFS at all (owner-only), and which
 *  GFSs carry its private channel. */
function PrivateGfs({ space, isRemoteSpace, onUpdate }: {
  space: Space
  isRemoteSpace: boolean
  onUpdate: () => void
}) {
  const [on, setOn] = useServerValue(Boolean(space.features?.private_gfs))
  const [linkMembers, setLinkMembers] = useState<LinkMemberHousehold[] | null>(null)
  const [channelError, setChannelError] = useState<string | null>(null)
  const [pick, setPick] = useState<string[] | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const choices = useRef<PrivateChannelChoices | null>(null)
  // Set when the owner just turned the GFS on: the picker's first load
  // then saves its pre-ticked server (never on a plain page visit).
  const justEnabled = useRef(false)
  const pickerRef = useRef<HTMLFieldSetElement | null>(null)

  const saveChannels = async (ids: string[]) => {
    try {
      const res = await api.put<{ listed: string[]; failed: { gfs_id: string }[] }>(
        `/api/spaces/${space.id}/gfs-servers`, { gfs_ids: ids },
      )
      const failedNames = (res?.failed ?? []).map(f =>
        choices.current?.choices.find(c => c.id === f.gfs_id)?.display_name ?? f.gfs_id)
      showToast(
        failedNames.length > 0
          ? t('space.private_gfs.channels_failed', { names: failedNames.join(', ') })
          : t('space.private_gfs.channels_saved'),
        failedNames.length > 0 ? 'info' : 'success',
      )
      setPick(null)
      setReloadKey(k => k + 1)
    } catch (e: unknown) {
      showToast((e as Error)?.message || t('space.settings.save_failed'), 'error')
      setPick(null)
    }
  }

  const flip = async (next: boolean) => {
    if (!next && !(await confirmDialog(t('space.private_gfs.off_note'), {
      title: t('space.settings_page.find.private_gfs_off_title'),
      confirmLabel: t('space.settings_page.find.private_gfs_off_ok'),
      destructive: true,
    }))) return
    setOn(next)
    setLinkMembers(null)
    const outcome = await patchSpace(space.id, { features: { private_gfs: next } }, {
      onUpdate, onLinkMembers: setLinkMembers,
    })
    if (outcome === 'failed') setOn(!next)
    else if (next) justEnabled.current = true
  }

  return (
    <div class="sh-private-gfs" data-testid="private-gfs">
      <p class="sh-ss-hint" data-testid="private-gfs-intro">{t('space.private_gfs.intro')}</p>
      <SwitchRow id={`private-gfs-${space.id}`} label={t('space.private_gfs.toggle')}
                 hint={on ? t('space.private_gfs.on_help') : t('space.private_gfs.off_help')}
                 checked={on} onChange={(v) => void flip(v)} />
      {on && !isRemoteSpace && (
        <PrivateChannelPicker
          ref={pickerRef}
          spaceId={space.id}
          selected={pick}
          reloadKey={reloadKey}
          error={channelError}
          onLoaded={(c) => {
            choices.current = c
            if (justEnabled.current) {
              justEnabled.current = false
              if (c.initial.length > 0 && !sameGfsSet(c.initial, c.saved)) {
                void saveChannels(c.initial)
              }
            }
          }}
          onChange={(ids) => {
            const loaded = choices.current
            if (ids.length === 0 && loaded && loaded.choices.length > 0) {
              setChannelError(t('space.private_gfs.channels_required'))
              revealGfsChecklist(pickerRef)
              return
            }
            setChannelError(null)
            setPick(ids)
            void saveChannels(ids)
          }}
        />
      )}
      <p class="sh-ss-hint">{t('space.private_gfs.owner_only')}</p>
      {linkMembers && (
        <div class="sh-private-gfs__blocked" role="alert" data-testid="private-gfs-link-members">
          <p class="sh-private-gfs__blocked-title">{t('space.private_gfs.link_members_title')}</p>
          <p class="sh-muted" style={{ margin: 0 }}>{t('space.private_gfs.link_members_body')}</p>
          <ul class="sh-private-gfs__households">
            {linkMembers.map(h => (
              <li key={h.instance_id}>
                <strong>{h.display_name}</strong>
                {h.members.length > 0 && (
                  <span class="sh-muted">{' — '}{h.members.map(m => m.display_name).join(', ')}</span>
                )}
              </li>
            ))}
          </ul>
          <a href={addBase(`/spaces/${space.id}/members`)} class="sh-private-gfs__members-link">
            {t('space.private_gfs.link_members_open')}
          </a>
        </div>
      )}
    </div>
  )
}

/** How members post through the GFS (v_50) — owner-only, and only where
 *  members publish over a GFS at all. A strict space keeps it visible so
 *  the owner can always switch back. */
function PublishMode({ space, onUpdate }: { space: Space; onUpdate: () => void }) {
  const stored = space.features?.gfs_publish_mode ?? 'trusted'
  const [mode, setMode] = useServerValue<GfsPublishMode>(stored)
  const isPrivate = space.space_type === 'private'
  const shown = isPrivate
    ? Boolean(space.features?.private_gfs)
    : ((space.space_type === 'public' || space.space_type === 'global')
      && Boolean(space.features?.allow_subscribers)) || stored !== 'trusted'
  if (!shown) return null

  const choose = async (next: GfsPublishMode) => {
    if (next === mode) return
    const before = mode
    setMode(next)
    const outcome = await patchSpace(space.id, { features: { gfs_publish_mode: next } }, {
      onUpdate,
      okToast: next === 'strict' ? t('space.gfs_publish.strict_switch_note') : undefined,
    })
    if (outcome === 'failed') setMode(before)
  }

  return (
    <fieldset class="sh-form-fieldset sh-gfs-publish-mode">
      <legend id={`gfs-publish-legend-${space.id}`}>{t('space.gfs_publish.legend')}</legend>
      <p class="sh-ss-hint">
        {isPrivate ? t('space.gfs_publish.private_intro') : t('space.gfs_publish.intro')}
      </p>
      <ChipRadioGroup<GfsPublishMode>
        labelledBy={`gfs-publish-legend-${space.id}`}
        value={mode}
        onChange={(v) => void choose(v)}
        options={[
          { value: 'trusted', label: t('space.gfs_publish.trusted') },
          { value: 'strict', label: t('space.gfs_publish.strict') },
        ]}
      />
      <dl class="sh-gfs-publish-mode__help">
        <dt>{t('space.gfs_publish.trusted')}</dt>
        <dd>{t('space.gfs_publish.trusted_help')}</dd>
        <dt>{t('space.gfs_publish.strict')}</dt>
        <dd>{t('space.gfs_publish.strict_help')}</dd>
      </dl>
    </fieldset>
  )
}
