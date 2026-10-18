import { t } from '@/i18n/i18n'

/** Id of the ``<main>`` landmark in ``App.tsx`` the link jumps to. */
const MAIN_ID = 'main'

/**
 * "Skip to main content" link (§23.21).
 *
 * The document always carries a ``<base href>`` — ``/`` standalone, or
 * ``/api/hassio_ingress/<token>/`` under HA ingress — and a fragment
 * href resolves against it, not the page URL: ``#main`` means
 * ``<base>#main``, the app root. Following it left the current page for
 * Home. The ingress-aware router skips hash-only links on purpose, so the
 * link handles itself: it moves focus to the ``<main>`` landmark (which
 * has ``tabIndex={-1}``) in place, without navigating.
 *
 * ``href`` stays so it is a real link — focusable, announced as one and
 * activated by Enter, which fires ``click``.
 */
export function SkipLink() {
  const onClick = (ev: MouseEvent) => {
    ev.preventDefault()
    const main = document.getElementById(MAIN_ID)
    // ``<main>`` spans the page under the sticky top bar, so it is always
    // in view; letting ``focus()`` scroll it would tuck the start of the
    // content under that bar.
    main?.focus({ preventScroll: true })
  }
  return (
    <a href={`#${MAIN_ID}`} class="sh-skip-link" onClick={onClick}>
      {t('a11y.skip_to_main')}
    </a>
  )
}
