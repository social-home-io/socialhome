/**
 * OnboardingFlow — first-run experience (§23.1/§23.92).
 *
 * Shown when currentUser.is_new_member is true. Each step pairs a
 * short tagline with a small illustrative mock — a feed post, a
 * shopping list, a sticky note, a pairing QR — so the operator sees
 * what the surface actually feels like, not just bullet points. The
 * mocks are inert; they exist only to set expectations.
 *
 * Admins get one more step at the end: "Connect to the GFS" — an opt-in,
 * UNCHECKED offer to pair with the household's default GFS
 * (``[gfs] default_url``) through its open sign-up. Whether to offer it
 * comes from ``GET /api/gfs/connections/default``, which answers from local
 * facts only; nothing reaches a GFS unless the admin ticks the box and
 * presses Connect (``POST /api/gfs/connections/default``).
 */
import { type ComponentChildren } from 'preact'
import { useEffect, useState } from 'preact/hooks'
import { api, ApiError } from '@/api'
import { FINAL_GFS_ERRORS, gfsConnectErrorText } from '@/features/connections/gfsErrors'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { Button } from './Button'

interface OnboardStep {
  /** i18n keys — resolved at render so the tour follows the UI language. */
  titleKey: string
  bodyKey: string
  illustration: () => ComponentChildren
}

/* For ``i18n:check``: t('onboarding.welcome.title') t('onboarding.welcome.body')
 * t('onboarding.feed.title') t('onboarding.feed.body') t('onboarding.lists.title')
 * t('onboarding.lists.body') t('onboarding.connect.title') t('onboarding.connect.body') */
const STEPS: OnboardStep[] = [
  {
    titleKey: 'onboarding.welcome.title',
    bodyKey: 'onboarding.welcome.body',
    illustration: () => (
      <div class="sh-onboard-illus">
        <div class="sh-onboard-card sh-onboard-card--welcome">
          <div class="sh-onboard-tape" aria-hidden="true" />
          <div class="sh-onboard-avatars">
            <span class="sh-onboard-avatar sh-onboard-avatar--a">M</span>
            <span class="sh-onboard-avatar sh-onboard-avatar--b">P</span>
            <span class="sh-onboard-avatar sh-onboard-avatar--c">L</span>
            <span class="sh-onboard-avatars-more">+2</span>
          </div>
          <div class="sh-onboard-card-title">{t('onboarding.welcome.illus_household')}</div>
          <div class="sh-onboard-card-meta">{t('onboarding.welcome.illus_meta')}</div>
        </div>
      </div>
    ),
  },
  {
    titleKey: 'onboarding.feed.title',
    bodyKey: 'onboarding.feed.body',
    illustration: () => (
      <div class="sh-onboard-illus">
        <div class="sh-onboard-card sh-onboard-card--feed">
          <div class="sh-onboard-tape sh-onboard-tape--moss" aria-hidden="true" />
          <div class="sh-onboard-row">
            <span class="sh-onboard-avatar sh-onboard-avatar--a">M</span>
            <div>
              <div class="sh-onboard-card-title">Maria</div>
              <div class="sh-onboard-card-meta">{t('onboarding.feed.illus_meta')}</div>
            </div>
          </div>
          <p class="sh-onboard-card-text">
            {t('onboarding.feed.illus_text')} 🍝 <em>{t('onboarding.feed.illus_em')}</em>
          </p>
          <div class="sh-onboard-reactions">
            <span>❤️ 3</span><span>🍝 2</span><span>💬 4</span>
          </div>
        </div>
      </div>
    ),
  },
  {
    titleKey: 'onboarding.lists.title',
    bodyKey: 'onboarding.lists.body',
    illustration: () => (
      <div class="sh-onboard-illus sh-onboard-illus--pair">
        <div class="sh-onboard-card sh-onboard-card--shop">
          <div class="sh-onboard-tape" aria-hidden="true" />
          <div class="sh-onboard-card-kicker">{t('nav.shopping')}</div>
          <ul class="sh-onboard-list">
            <li class="is-done"><span class="sh-onboard-tick" /> {t('onboarding.lists.illus_bread')}</li>
            <li class="is-done"><span class="sh-onboard-tick" /> {t('onboarding.lists.illus_oil')}</li>
            <li><span class="sh-onboard-tick sh-onboard-tick--empty" /> {t('onboarding.lists.illus_tomatoes')} <em>+ Maria</em></li>
            <li><span class="sh-onboard-tick sh-onboard-tick--empty" /> {t('onboarding.lists.illus_basil')} <em>+ Pascal</em></li>
          </ul>
        </div>
        <div class="sh-onboard-card sh-onboard-card--cal">
          <div class="sh-onboard-tape sh-onboard-tape--moss" aria-hidden="true" />
          <div class="sh-onboard-card-kicker">{t('onboarding.lists.illus_date')}</div>
          <div class="sh-onboard-card-title">{t('onboarding.lists.illus_event')}</div>
          <div class="sh-onboard-card-meta">{t('onboarding.lists.illus_event_meta')}</div>
        </div>
      </div>
    ),
  },
  {
    titleKey: 'onboarding.connect.title',
    bodyKey: 'onboarding.connect.body',
    illustration: () => (
      <div class="sh-onboard-illus">
        <div class="sh-onboard-card sh-onboard-card--qr">
          <div class="sh-onboard-tape sh-onboard-tape--honey" aria-hidden="true" />
          <div class="sh-onboard-card-kicker">{t('onboarding.connect.illus_kicker')}</div>
          <div class="sh-onboard-qr" aria-hidden="true">
            <div class="sh-onboard-qr-grid">
              {Array.from({ length: 49 }, (_, i) => (
                <span
                  key={i}
                  class={
                    [0, 6, 42, 48, 8, 12, 16, 19, 24, 28, 32, 36, 40].includes(i % 49)
                      ? 'sh-onboard-qr-cell is-on'
                      : 'sh-onboard-qr-cell'
                  }
                />
              ))}
            </div>
          </div>
          <div class="sh-onboard-card-meta">🔒 {t('onboarding.connect.illus_meta')}</div>
        </div>
      </div>
    ),
  },
]


/** ``GET /api/gfs/connections/default``. Only ``available: true`` earns
 *  the step; any ``reason`` (``disabled``, ``already_connected``, or one
 *  an older backend still sends) means no step. */
interface GfsOffer {
  url: string
  available: boolean
  reason: string | null
}

type GfsState = 'idle' | 'connecting' | 'active' | 'pending' | 'error'

/** A failed connect: the code (drives whether Try again is pointless —
 *  see ``FINAL_GFS_ERRORS``) and the line to show for it. */
interface GfsFailure {
  code: string | null
  text: string
}

function hostOf(url: string): string {
  try {
    return new URL(url).host || url
  } catch {
    return url
  }
}

function GfsIllustration({ host }: { host: string }) {
  return (
    <div class="sh-onboard-illus">
      <div class="sh-onboard-card sh-onboard-card--gfs">
        <div class="sh-onboard-tape sh-onboard-tape--moss" aria-hidden="true" />
        <div class="sh-onboard-card-kicker">GFS</div>
        <div class="sh-onboard-card-title">{host}</div>
        <ul class="sh-onboard-list">
          <li><span class="sh-onboard-tick" /> {t('onboarding.gfs.illus_links')}</li>
          <li><span class="sh-onboard-tick" /> {t('onboarding.gfs.illus_public')}</li>
          <li><span class="sh-onboard-tick" /> {t('onboarding.gfs.illus_offline')}</li>
        </ul>
        <div class="sh-onboard-card-meta">🔒 {t('onboarding.gfs.illus_meta')}</div>
      </div>
    </div>
  )
}

interface GfsStepProps {
  offer: GfsOffer
  wanted: boolean
  state: GfsState
  failure: GfsFailure | null
  onWantedChange: (next: boolean) => void
}

function GfsStepBody({ offer, wanted, state, failure, onWantedChange }: GfsStepProps) {
  const done = state === 'active' || state === 'pending'
  const closed = state === 'error' && failure?.code != null && FINAL_GFS_ERRORS.has(failure.code)
  return (
    <div class="sh-onboarding-gfs">
      <p class="sh-onboarding-body">{t('onboarding.gfs.intro')}</p>
      <div class="sh-onboarding-gfs-facts">
        <section>
          <h3>{t('onboarding.gfs.enables_heading')}</h3>
          <ul>
            <li>{t('onboarding.gfs.enables_links')}</li>
            <li>{t('onboarding.gfs.enables_public')}</li>
            <li>{t('onboarding.gfs.enables_offline')}</li>
          </ul>
        </section>
        <section>
          <h3>{t('onboarding.gfs.sees_heading')}</h3>
          <p>{t('onboarding.gfs.sees_body')}</p>
        </section>
      </div>
      <label class="sh-onboarding-gfs-check">
        <input
          type="checkbox"
          checked={wanted}
          disabled={done || closed || state === 'connecting'}
          onChange={(e) => onWantedChange((e.target as HTMLInputElement).checked)}
        />
        <span>{t('onboarding.gfs.checkbox', { server: hostOf(offer.url) })}</span>
      </label>
      {state === 'active' && (
        <p class="sh-onboarding-gfs-note sh-onboarding-gfs-note--ok" role="status">
          {t('onboarding.gfs.success')}
        </p>
      )}
      {state === 'pending' && (
        <p class="sh-onboarding-gfs-note sh-onboarding-gfs-note--wait" role="status">
          {t('onboarding.gfs.pending')}
        </p>
      )}
      {state === 'error' && failure && (
        <>
          <p class="sh-onboarding-gfs-note sh-onboarding-gfs-note--error" role="alert">
            {failure.text}
          </p>
          <p class="sh-onboarding-gfs-note">{t('onboarding.gfs.error_later')}</p>
        </>
      )}
      {!done && state !== 'error' && (
        <p class="sh-onboarding-gfs-note">{t('onboarding.gfs.default_note')}</p>
      )}
    </div>
  )
}

export function OnboardingFlow({ onComplete }: { onComplete: () => void }) {
  const [step, setStep] = useState(0)
  const [gfsOffer, setGfsOffer] = useState<GfsOffer | null>(null)
  const [gfsWanted, setGfsWanted] = useState(false)
  const [gfsState, setGfsState] = useState<GfsState>('idle')
  const [gfsFailure, setGfsFailure] = useState<GfsFailure | null>(null)
  const isAdmin = !!currentUser.value?.is_admin

  // Only admins can pair, and only when a default GFS is configured. The
  // answer comes from this household alone — asking never contacts the GFS.
  // Already connected (or no default, or no answer at all): no step.
  useEffect(() => {
    if (!isAdmin) return
    let alive = true
    api.get('/api/gfs/connections/default')
      .then((body) => {
        const offer = body as GfsOffer
        if (!alive || !offer?.url) return
        if (offer.available) setGfsOffer(offer)
      })
      .catch(() => { /* no step — onboarding must never block on this */ })
    return () => { alive = false }
  }, [isAdmin])

  const total = STEPS.length + (gfsOffer ? 1 : 0)
  const onGfsStep = gfsOffer !== null && step === STEPS.length
  const current = STEPS[Math.min(step, STEPS.length - 1)]
  const isLast = step === total - 1

  // Both "Let's go" and "Skip tour" mark the wizard done. ``App.tsx``
  // gates the wizard on ``currentUser.is_new_member``, so we mirror the
  // server-side flag flip onto the local ``currentUser`` signal —
  // otherwise the App's next render re-flips ``showOnboarding`` to
  // ``true`` and the dialog reappears, making both buttons look like
  // they "do nothing".
  const finish = () => {
    api.post('/api/me/onboarding-complete').catch(() => {})
    if (currentUser.value) {
      currentUser.value = { ...currentUser.value, is_new_member: false }
    }
    onComplete()
  }

  const connectGfs = async () => {
    setGfsState('connecting')
    setGfsFailure(null)
    try {
      const conn = await api.post('/api/gfs/connections/default') as { status?: string }
      setGfsState(conn?.status === 'pending' ? 'pending' : 'active')
    } catch (err) {
      const code = err instanceof ApiError ? err.code : null
      if (code === 'ALREADY_CONNECTED') {
        setGfsState('active')
        return
      }
      // A closed sign-up or the wrong server won't change on a retry:
      // untick, so the primary button just finishes.
      if (code !== null && FINAL_GFS_ERRORS.has(code)) setGfsWanted(false)
      setGfsFailure({ code, text: gfsConnectErrorText(err) })
      setGfsState('error')
    }
  }

  // On the GFS step a ticked box turns the primary button into Connect
  // (or Try again); unticked — the default — it just finishes.
  const gfsNeedsConnect = onGfsStep && !!gfsOffer?.available && gfsWanted
    && (gfsState === 'idle' || gfsState === 'error')

  const next = () => {
    if (onGfsStep && gfsState === 'connecting') return
    if (gfsNeedsConnect) {
      void connectGfs()
    } else if (isLast) {
      finish()
    } else {
      setStep(step + 1)
    }
  }

  const back = () => {
    if (step > 0 && gfsState !== 'connecting') setStep(step - 1)
  }

  const skip = () => {
    finish()
  }

  // Keyboard nav so power users (or anyone on a keyboard-only device)
  // can step through without grabbing the mouse: ←/→ flip steps,
  // Esc skips the tour. Bound on `window` because the dialog isn't
  // focus-trapped — the tour is a peer of the main UI, not a modal
  // gate.
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'ArrowRight') { ev.preventDefault(); next() }
      else if (ev.key === 'ArrowLeft') { ev.preventDefault(); back() }
      else if (ev.key === 'Escape')   { ev.preventDefault(); skip() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [step, total, gfsWanted, gfsState, gfsOffer])

  let primaryLabel = isLast ? t('onboarding.finish') : t('onboarding.next')
  if (onGfsStep && gfsState === 'connecting') primaryLabel = t('onboarding.gfs.connecting')
  else if (gfsNeedsConnect) {
    primaryLabel = gfsState === 'error' ? t('onboarding.gfs.retry') : t('onboarding.gfs.connect')
  }

  return (
    <div class="sh-onboarding" role="dialog" aria-labelledby="sh-onboarding-title">
      <div class="sh-onboarding-card">
        <div class="sh-onboarding-illustration">
          {onGfsStep && gfsOffer
            ? <GfsIllustration host={hostOf(gfsOffer.url)} />
            : current.illustration()}
        </div>
        <h2 id="sh-onboarding-title" class="sh-onboarding-title">
          {onGfsStep ? t('onboarding.gfs.title') : t(current.titleKey)}
        </h2>
        {onGfsStep && gfsOffer ? (
          <GfsStepBody
            offer={gfsOffer}
            wanted={gfsWanted}
            state={gfsState}
            failure={gfsFailure}
            onWantedChange={(v) => {
              setGfsWanted(v)
              if (!v && gfsState === 'error') setGfsState('idle')
            }}
          />
        ) : (
          <p class="sh-onboarding-body">{t(current.bodyKey)}</p>
        )}
        <div
          class="sh-onboarding-dots"
          role="progressbar"
          aria-valuemin={1}
          aria-valuemax={total}
          aria-valuenow={step + 1}
          aria-label={t('onboarding.step_of', { n: String(step + 1), total: String(total) })}
        >
          {Array.from({ length: total }, (_, i) => (
            <span
              key={i}
              class={i === step ? 'sh-dot sh-dot--active' : 'sh-dot'}
            />
          ))}
        </div>
        <div class="sh-onboarding-actions">
          <Button variant="secondary" onClick={skip}>{t('onboarding.skip')}</Button>
          <div class="sh-onboarding-actions-right">
            {step > 0 && (
              <Button variant="secondary" onClick={back}
                      disabled={gfsState === 'connecting'}>
                {t('onboarding.back')}
              </Button>
            )}
            <Button onClick={next} disabled={onGfsStep && gfsState === 'connecting'}>
              {primaryLabel}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
