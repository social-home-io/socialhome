/**
 * SpaceSetupCard — "What is <name> for?", on a new space's Feed for its
 * owner until they choose or skip (``spaceSetup``).
 *
 * - **Start from** is a real radio group in a ``<fieldset>``: Family ·
 *   Club or group · Neighbours · Just a feed. Nothing is picked unless
 *   the name (or category) gives the purpose away.
 * - **Sections** are toggle buttons (``aria-pressed``) in the app's one
 *   selected style (primary-soft tint + primary border). Picking a
 *   preset sets them; any chip can be flipped after.
 * - **Done** saves exactly the chips shown (one features PATCH) and the
 *   card is gone for good; **Skip** keeps the space as it is (feed, chat,
 *   calendar) and is gone for good too. Settings › Sections changes it
 *   any time — the lead line says so.
 */
import { useId, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { Button } from '@/components/Button'
import { patchSpace } from './settings/spacePatch'
import {
  PRESET_EMOJI, PRESET_SECTIONS, SETUP_PRESETS, SETUP_SECTIONS,
  clearSetupPending, featuresFor, guessPreset, presetHint, presetLabel,
  sectionsOn, setupSectionLabel, type SetupPreset, type SetupSection,
} from './spaceSetup'

interface Props {
  spaceId: string
  name: string
  category?: string | null
  /** The space's current features (the server's new-space default). */
  features: Record<string, unknown> | undefined
  /** The choice was saved or skipped — the page drops the card and
   *  re-reads the space. */
  onDone: () => void
}

export function SpaceSetupCard({ spaceId, name, category, features, onDone }: Props) {
  const ids = useId()
  const guess = guessPreset(name, category)
  const [preset, setPreset] = useState<SetupPreset | null>(guess)
  const [on, setOn] = useState<SetupSection[]>(
    () => (guess ? [...PRESET_SECTIONS[guess]] : sectionsOn(features)),
  )
  const [busy, setBusy] = useState(false)

  const pick = (p: SetupPreset) => {
    setPreset(p)
    setOn([...PRESET_SECTIONS[p]])
  }
  const flip = (s: SetupSection) => {
    setOn(on.includes(s) ? on.filter((x) => x !== s) : [...on, s])
  }

  const finish = async (save: boolean) => {
    setBusy(true)
    try {
      if (save) {
        const outcome = await patchSpace(
          spaceId, { features: featuresFor(on) }, { okToast: t('space.setup.saved') },
        )
        if (outcome === 'failed') return
      }
      // A hint: if it fails, the card just asks again next time.
      try { await clearSetupPending(spaceId) } catch { /* asks again */ }
      onDone()
    } finally {
      setBusy(false)
    }
  }

  return (
    <section class="sh-space-setup" aria-labelledby={`${ids}-h`}>
      <h2 id={`${ids}-h`} class="sh-space-setup__title">{t('space.setup.title', { name })}</h2>
      <p class="sh-space-setup__lead">{t('space.setup.lead')}</p>

      <fieldset class="sh-space-setup__presets">
        <legend>{t('space.setup.start_from')}</legend>
        <div class="sh-space-setup__preset-grid">
          {SETUP_PRESETS.map((p) => (
            <label key={p} class={`sh-space-setup__preset${preset === p ? ' is-on' : ''}`}>
              <input type="radio" name={`${ids}-preset`} value={p}
                     checked={preset === p} onChange={() => pick(p)} />
              <span class="sh-space-setup__emoji" aria-hidden="true">{PRESET_EMOJI[p]}</span>
              <span class="sh-space-setup__name">{presetLabel(p)}</span>
              <span class="sh-space-setup__hint">{presetHint(p)}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div class="sh-space-setup__sections" role="group" aria-labelledby={`${ids}-s`}>
        <span id={`${ids}-s`} class="sh-space-setup__label">{t('space.setup.sections')}</span>
        <div class="sh-space-setup__chips">
          {SETUP_SECTIONS.map((s) => {
            const pressed = on.includes(s)
            return (
              <button key={s} type="button" class="sh-space-setup__chip" aria-pressed={pressed}
                      onClick={() => flip(s)}>
                <span aria-hidden="true">{pressed ? '✓' : '+'}</span> {setupSectionLabel(s)}
              </button>
            )
          })}
        </div>
      </div>

      <div class="sh-space-setup__actions">
        <Button variant="secondary" onClick={() => { void finish(false) }} disabled={busy}>
          {t('space.setup.skip')}
        </Button>
        <Button onClick={() => { void finish(true) }} loading={busy}>
          {t('space.setup.done')}
        </Button>
      </div>
    </section>
  )
}
