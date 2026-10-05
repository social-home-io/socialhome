/**
 * InstallPrompt — PWA install banner (§23.114).
 *
 * Captures the browser's ``beforeinstallprompt`` event and renders a
 * small banner inviting the user to install Social Home to their home
 * screen. Once the banner is dismissed (or the install completes) we
 * persist the decision in ``localStorage`` so we don't re-show it on
 * every load.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { Button } from './Button'
import { t } from '@/i18n/i18n'

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

const DISMISS_KEY = 'sh-install-dismissed'

const deferredPrompt = signal<BeforeInstallPromptEvent | null>(null)
const visible = signal(false)


function _shouldShow(): boolean {
  try {
    return localStorage.getItem(DISMISS_KEY) !== '1'
  } catch {
    return true
  }
}


function _markDismissed(): void {
  try {
    localStorage.setItem(DISMISS_KEY, '1')
  } catch {
    // localStorage disabled — fine, banner just re-shows next session.
  }
}


export function InstallPrompt() {
  useEffect(() => {
    function onBeforeInstallPrompt(e: Event) {
      e.preventDefault()
      if (!_shouldShow()) return
      deferredPrompt.value = e as BeforeInstallPromptEvent
      visible.value = true
    }
    function onInstalled() {
      visible.value = false
      _markDismissed()
    }
    window.addEventListener('beforeinstallprompt', onBeforeInstallPrompt)
    window.addEventListener('appinstalled', onInstalled)
    return () => {
      window.removeEventListener('beforeinstallprompt', onBeforeInstallPrompt)
      window.removeEventListener('appinstalled', onInstalled)
    }
  }, [])

  if (!visible.value || !deferredPrompt.value) return null

  const handleInstall = async () => {
    const evt = deferredPrompt.value
    if (!evt) return
    await evt.prompt()
    await evt.userChoice
    deferredPrompt.value = null
    visible.value = false
    _markDismissed()
  }

  const handleDismiss = () => {
    visible.value = false
    deferredPrompt.value = null
    _markDismissed()
  }

  return (
    <div class="sh-install-prompt" role="dialog" aria-label={t('install.aria')}>
      <span class="sh-install-prompt-text">{t('install.text')}</span>
      <Button onClick={handleInstall}>{t('install.button')}</Button>
      <Button variant="secondary" onClick={handleDismiss}>{t('common.not_now')}</Button>
    </div>
  )
}
