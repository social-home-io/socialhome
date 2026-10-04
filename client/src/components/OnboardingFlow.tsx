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
import { t } from '@/i18n/i18n'
import { isSupervisorAddon } from '@/platform'
import { currentUser } from '@/store/auth'
import { Button } from './Button'

interface OnboardStep {
  title: string
  body: string
  illustration: () => ComponentChildren
}

const STEPS: OnboardStep[] = [
  {
    title: 'Welcome to Social Home',
    body: "Your private household — a feed, calendar, tasks, shopping, photos, and calls, all running on your own server and connected to Home Assistant.",
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
          <div class="sh-onboard-card-title">The Vizeli household</div>
          <div class="sh-onboard-card-meta">5 members · paired with 3 households</div>
        </div>
      </div>
    ),
  },
  {
    title: 'A feed for the people who actually live here',
    body: 'Post photos, polls, and updates that stay inside your household. No ads, no algorithm — just the people you live with.',
    illustration: () => (
      <div class="sh-onboard-illus">
        <div class="sh-onboard-card sh-onboard-card--feed">
          <div class="sh-onboard-tape sh-onboard-tape--moss" aria-hidden="true" />
          <div class="sh-onboard-row">
            <span class="sh-onboard-avatar sh-onboard-avatar--a">M</span>
            <div>
              <div class="sh-onboard-card-title">Maria</div>
              <div class="sh-onboard-card-meta">posted in Family · 4m</div>
            </div>
          </div>
          <p class="sh-onboard-card-text">
            Pasta night again? 🍝 New recipe from grandma —
            calling it: <em>everyone’s in by 19:00.</em>
          </p>
          <div class="sh-onboard-reactions">
            <span>❤️ 3</span><span>🍝 2</span><span>💬 4</span>
          </div>
        </div>
      </div>
    ),
  },
  {
    title: 'Shared lists, calendar, and chores',
    body: "Shopping list at the door, calendar at the fridge, tasks split between everyone — all live, all visible from any phone or tablet you've signed in on.",
    illustration: () => (
      <div class="sh-onboard-illus sh-onboard-illus--pair">
        <div class="sh-onboard-card sh-onboard-card--shop">
          <div class="sh-onboard-tape" aria-hidden="true" />
          <div class="sh-onboard-card-kicker">Shopping</div>
          <ul class="sh-onboard-list">
            <li class="is-done"><span class="sh-onboard-tick" /> Sourdough</li>
            <li class="is-done"><span class="sh-onboard-tick" /> Olive oil</li>
            <li><span class="sh-onboard-tick sh-onboard-tick--empty" /> Tomatoes <em>+ Maria</em></li>
            <li><span class="sh-onboard-tick sh-onboard-tick--empty" /> Basil <em>+ Pascal</em></li>
          </ul>
        </div>
        <div class="sh-onboard-card sh-onboard-card--cal">
          <div class="sh-onboard-tape sh-onboard-tape--moss" aria-hidden="true" />
          <div class="sh-onboard-card-kicker">Tue · Jul 29</div>
          <div class="sh-onboard-card-title">Sunday brunch @ Maria's</div>
          <div class="sh-onboard-card-meta">3 households joining</div>
        </div>
      </div>
    ),
  },
  {
    title: 'Federated, end-to-end encrypted',
    body: "Connect with other households over a QR code. Every message, photo, and event is encrypted in transit — your data lives on your server.",
    illustration: () => (
      <div class="sh-onboard-illus">
        <div class="sh-onboard-card sh-onboard-card--qr">
          <div class="sh-onboard-tape sh-onboard-tape--honey" aria-hidden="true" />
          <div class="sh-onboard-card-kicker">Pair a household</div>
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
          <div class="sh-onboard-card-meta">🔒 Ed25519 · expires in 5:00</div>
        </div>
      </div>
    ),
  },
]


/** ``GET /api/gfs/connections/default``. */
interface GfsOffer {
  url: string
  available: boolean
  reason: 'disabled' | 'no_external_url' | 'already_connected' | null
}

type GfsState = 'idle' | 'connecting' | 'active' | 'pending' | 'error'

/** Error code from ``POST /api/gfs/connections/default`` → our own words
 *  (never the server's detail, which may quote the GFS). */
function gfsErrorText(code: string | null): string {
  switch (code) {
    case 'GFS_UNREACHABLE': return t('onboarding.gfs.error_unreachable')
    case 'GFS_SIGNUP_CLOSED': return t('onboarding.gfs.error_closed')
    case 'GFS_BUSY': return t('onboarding.gfs.error_busy')
    case 'NOT_CONFIGURED': return noExternalUrlText()
    default: return t('onboarding.gfs.error_refused')
  }
}

/** Where the External URL comes from differs by mode: an admin types it
 *  in standalone / ha; under the add-on Home Assistant supplies it. */
function noExternalUrlText(): string {
  return isSupervisorAddon()
    ? t('onboarding.gfs.no_external_url_ha')
    : t('onboarding.gfs.no_external_url')
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
  errorCode: string | null
  onWantedChange: (next: boolean) => void
}

function GfsStepBody({ offer, wanted, state, errorCode, onWantedChange }: GfsStepProps) {
  const canPair = offer.available
  const done = state === 'active' || state === 'pending'
  const closed = state === 'error' && errorCode === 'GFS_SIGNUP_CLOSED'
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
      <label class={canPair ? 'sh-onboarding-gfs-check' : 'sh-onboarding-gfs-check is-disabled'}>
        <input
          type="checkbox"
          checked={wanted}
          disabled={!canPair || done || closed || state === 'connecting'}
          onChange={(e) => onWantedChange((e.target as HTMLInputElement).checked)}
        />
        <span>{t('onboarding.gfs.checkbox', { server: hostOf(offer.url) })}</span>
      </label>
      {!canPair && (
        <p class="sh-onboarding-gfs-note sh-onboarding-gfs-note--hint">{noExternalUrlText()}</p>
      )}
      {canPair && state === 'active' && (
        <p class="sh-onboarding-gfs-note sh-onboarding-gfs-note--ok" role="status">
          {t('onboarding.gfs.success')}
        </p>
      )}
      {canPair && state === 'pending' && (
        <p class="sh-onboarding-gfs-note sh-onboarding-gfs-note--wait" role="status">
          {t('onboarding.gfs.pending')}
        </p>
      )}
      {canPair && state === 'error' && (
        <p class="sh-onboarding-gfs-note sh-onboarding-gfs-note--error" role="alert">
          {gfsErrorText(errorCode)}
        </p>
      )}
      {canPair && !done && state !== 'error' && (
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
  const [gfsError, setGfsError] = useState<string | null>(null)
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
        if (offer.available || offer.reason === 'no_external_url') setGfsOffer(offer)
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
    setGfsError(null)
    try {
      const conn = await api.post('/api/gfs/connections/default') as { status?: string }
      setGfsState(conn?.status === 'pending' ? 'pending' : 'active')
    } catch (err) {
      const code = err instanceof ApiError ? err.code : null
      if (code === 'ALREADY_CONNECTED') {
        setGfsState('active')
        return
      }
      // A closed sign-up won't open on a retry: untick, so the primary
      // button just finishes and the message points at the pairing code.
      if (code === 'GFS_SIGNUP_CLOSED') setGfsWanted(false)
      setGfsError(code)
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
          {onGfsStep ? t('onboarding.gfs.title') : current.title}
        </h2>
        {onGfsStep && gfsOffer ? (
          <GfsStepBody
            offer={gfsOffer}
            wanted={gfsWanted}
            state={gfsState}
            errorCode={gfsError}
            onWantedChange={(v) => {
              setGfsWanted(v)
              if (!v && gfsState === 'error') setGfsState('idle')
            }}
          />
        ) : (
          <p class="sh-onboarding-body">{current.body}</p>
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
