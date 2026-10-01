import { render } from 'preact'
import { App } from './App'
import { SpaUpdateBanner } from './components/SpaUpdateBanner'
import { ws } from './ws'
import { setUnauthorizedHandler } from './api'
import { logout, onLogout } from './store/auth'
import './styles/tokens.css'
import './styles/app.css'
// Eagerly initialise the theme signal + effect so the `<html>` class
// is set on cold start, not on first /settings visit. The inline
// pre-paint script in index.html handles the very-first paint; this
// keeps the signal in sync for live toggles and system-theme flips.
import './store/theme'
import { wireFeedWs } from './store/feed'
import { wireShoppingWs, resetShopping } from './store/shopping'
import { wireCalendarWs } from './store/calendar'
import { wireTasksWs, resetTasks } from './store/tasks'
import { wireNotificationsWs } from './store/notifications'
import { wirePresenceWs, loadPresence } from './store/presence'
import { wireStickiesWs, resetStickies } from './store/stickies'
import { wireDmWs } from './store/dms'
import { wireCallsWs } from './store/calls'
import { wireConnectionsWs } from './store/connections'
import { wireUserPreferencesWs } from './store/userPreferences'
import { wireLocalePreference } from './store/localePreference'
import { initLocale } from './i18n/i18n'
import { wireMediaReadyWs } from './store/mediaReady'
import { wireSpacesWs } from './store/spaces'
import { wireProtectionWs } from './store/protection'

// Wire WebSocket event handlers to local stores BEFORE connecting so
// no events get lost between connect() and the subscribe() calls.
wireFeedWs()
wireShoppingWs()
wireCalendarWs()
wireTasksWs()
wireNotificationsWs()
wirePresenceWs()
// Seed the shared presence store from GET so author online-pills resolve
// by user_id before any WS frame arrives (session-presence frames carry
// no username). Upsert-merge — safe to fire alongside the WS wiring.
void loadPresence()
wireStickiesWs()
wireDmWs()
wireCallsWs()
wireConnectionsWs()
wireUserPreferencesWs()
wireLocalePreference()
void initLocale()
wireMediaReadyWs()
wireSpacesWs()
wireProtectionWs()

// Wire the api client's 401 handler to clear the session. Done here (not at
// store/auth module load) so api.ts stays free of a static import back to
// store/auth — that's what keeps the api↔auth dependency graph acyclic.
setUnauthorizedHandler(logout)
// Signing out forgets the household's shopping list (items, stores,
// pending Undo deletes), task lists and sticky count so the next
// account never sees them.
onLogout(resetShopping)
onLogout(resetTasks)
onLogout(resetStickies)

ws.connect()

render(<App />, document.getElementById('root')!)

// Mount the SPA-update banner OUTSIDE the App root so it survives
// the App's auth-gate early returns. A user who left the login
// page open across a backend deploy should still see the prompt;
// gating on ``authed.value`` (where the banner used to live)
// would have hidden it from them.
const _bannerRoot = document.createElement('div')
_bannerRoot.id = 'spa-update-banner-root'
document.body.appendChild(_bannerRoot)
render(<SpaUpdateBanner />, _bannerRoot)
