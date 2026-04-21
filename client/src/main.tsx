import { render } from 'preact'
import { App } from './App'
import { ws } from './ws'
import './styles/tokens.css'
import './styles/app.css'
import { wireFeedWs } from './store/feed'
import { wireShoppingWs } from './store/shopping'
import { wireGalleryWs } from './store/gallery'
import { wireCalendarWs } from './store/calendar'
import { wireTasksWs } from './store/tasks'
import { wireNotificationsWs } from './store/notifications'
import { wirePresenceWs } from './store/presence'
import { wireStickiesWs } from './store/stickies'
import { wireDmWs } from './store/dms'
import { wireCallsWs } from './store/calls'
import { wireConnectionsWs } from './store/connections'

// Wire WebSocket event handlers to local stores BEFORE connecting so
// no events get lost between connect() and the subscribe() calls.
wireFeedWs()
wireShoppingWs()
wireGalleryWs()
wireCalendarWs()
wireTasksWs()
wireNotificationsWs()
wirePresenceWs()
wireStickiesWs()
wireDmWs()
wireCallsWs()
wireConnectionsWs()
ws.connect()

render(<App />, document.getElementById('root')!)
