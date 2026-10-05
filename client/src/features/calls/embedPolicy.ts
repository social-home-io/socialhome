/**
 * embedPolicy — tell "the embedding page won't let us use the mic" apart
 * from "the user said no".
 *
 * A frame only gets the microphone / camera when its parent allows it.
 * Two Home Assistant shapes matter (home-assistant/frontend @16183f9):
 *
 * - The add-on **ingress panel** (``src/panels/app/ha-panel-app.ts``)
 *   renders ``<iframe src=${addon.ingress_url}>`` with no ``allow``
 *   attribute. ``ingress_url`` is ``/api/hassio_ingress/<token>/`` on
 *   HA's own origin, so the frame is same-origin with its parent and
 *   inherits the default ``'self'`` allowlist — calls work there.
 * - A **Webpage dashboard / panel_iframe** (``src/panels/iframe/
 *   ha-panel-iframe.ts``) and the **iframe card** (``hui-iframe-card.ts``,
 *   default) set ``allow="fullscreen"``. Pointed at Social Home's direct
 *   URL they are cross-origin, and ``X-Frame-Options: SAMEORIGIN`` keeps
 *   Social Home out of them altogether. Only a frame whose ancestors are
 *   all same-origin can render us, so a denial reaches this module when a
 *   same-origin ancestor narrows the policy (``allow="microphone 'none'"``,
 *   a ``Permissions-Policy`` header on the parent) — rare, but the one
 *   case a retry can't fix.
 *
 * Chromium exposes the effective policy (``document.permissionsPolicy`` /
 * the older ``featurePolicy``), so the denial is known up front. Firefox
 * and Safari don't, so a framed ``NotAllowedError`` is reported as a
 * possible embed denial rather than a certain one.
 */

import { t } from '@/i18n/i18n'

interface PolicyApi { allowsFeature(feature: string): boolean }

export class CallEmbedBlockedError extends Error {
  /** ``true`` when the browser confirmed the policy denial; ``false``
   *  when it is only the likely cause of a framed ``NotAllowedError``. */
  readonly certain: boolean

  constructor(opts: { cause?: unknown, certain?: boolean } = {}) {
    const certain = opts.certain ?? true
    super(
      certain
        ? t('calls.embed.blocked_certain')
        : t('calls.embed.blocked_maybe'),
      { cause: opts.cause },
    )
    this.name = 'CallEmbedBlockedError'
    this.certain = certain
  }
}

/** ``true`` inside any frame (a cross-origin parent throws on access). */
export function isFramed(): boolean {
  try {
    return window.self !== window.top
  } catch {
    return true
  }
}

/** ``true`` only when the browser says the embedding page denies the
 *  microphone to this document. ``false`` when unframed or unknown. */
export function embedBlocksMicrophone(): boolean {
  if (!isFramed()) return false
  const doc = document as Document & {
    permissionsPolicy?: PolicyApi, featurePolicy?: PolicyApi,
  }
  const policy = doc.permissionsPolicy ?? doc.featurePolicy
  if (typeof policy?.allowsFeature !== 'function') return false
  try {
    return !policy.allowsFeature('microphone')
  } catch {
    return false
  }
}

/** The page the user is on, to reopen in a top-level tab. Under ingress
 *  this is the ``/api/hassio_ingress/<token>/…`` URL on HA's origin — the
 *  frontend sets the ``ingress_session`` cookie on
 *  ``path=/api/hassio_ingress/`` (``src/data/hassio/ingress.ts``), so it
 *  loads in its own tab too; for a
 *  cross-origin embed it is Social Home's direct URL. */
export function ownTabUrl(): string {
  return window.location.href
}
