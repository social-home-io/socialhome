/* Service worker for push notifications (§23.33, §25.3).
 *
 * Payload (``socialhome/services/push_service.py`` ``PushPayload``):
 * ``{title, click_url?, tag?, space_id?, icon_url?}`` — title only, never a
 * body. ``url`` is still read as a fallback for ``click_url``. A withdraw
 * (``{withdraw: [tags]}``) closes earlier notifications instead.
 *
 * URL resolution under HA Supervisor ingress
 * ------------------------------------------
 * The worker is registered with a relative URL so its scope tracks the
 * document base. Under ingress that is ``/api/hassio_ingress/<token>/``;
 * outside ingress it is ``/``. Any URL we open from inside the worker is
 * resolved against ``registration.scope`` so notifications open Social
 * Home, not HA Core — and a link that would leave the scope opens the app
 * root instead.
 */
function resolveInScope(target) {
  const scope = self.registration.scope
  // Strip leading slashes so server-supplied paths like ``/feed`` resolve
  // **inside** the worker's scope rather than escaping to the origin root.
  const rel = String(target || '').replace(/^\/+/, '')
  let url
  try {
    url = new URL(rel, scope).href
  } catch {
    return scope
  }
  return url.startsWith(scope) ? url : scope
}

/* Withdraw (``{withdraw: [tags], tag}``): the things these notifications
 * were about were handled elsewhere — read on another device, answered by
 * another admin, a call picked up. Close them and show nothing.
 *
 * The server only sends a withdraw to Chromium-family push services, where
 * a push that leaves nothing on screen spends a silent-push budget rather
 * than costing the subscription (Safari revokes it, Firefox spends a quota
 * — see ``tolerates_silent_push`` in ``push_service.py``). ``tag`` is for
 * an older worker that doesn't know withdraws; this one ignores it.
 */
async function withdraw(tags) {
  const wanted = new Set(tags.map((t) => String(t)))
  const shown = await self.registration.getNotifications()
  for (const n of shown) {
    if (n.tag && wanted.has(n.tag)) n.close()
  }
}

self.addEventListener('push', (event) => {
  let data = {}
  try {
    data = (event.data && event.data.json()) || {}
  } catch {
    data = {}
  }
  if (Array.isArray(data.withdraw)) {
    event.waitUntil(withdraw(data.withdraw))
    return
  }
  const title = data.title || 'Social Home'
  const options = {
    body: data.body || '',
    data: { url: resolveInScope(data.click_url || data.url || '') },
  }
  if (data.tag) {
    // One notification per item on the device: a repeat (a DM burst)
    // replaces the previous one quietly instead of stacking up.
    options.tag = String(data.tag)
    options.renotify = false
  }
  event.waitUntil(self.registration.showNotification(title, options))
})

async function openOrFocus(url) {
  const scope = self.registration.scope
  const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
  const ours = windows.filter((c) => typeof c.url === 'string' && c.url.startsWith(scope))
  const exact = ours.find((c) => c.url === url)
  if (exact) return exact.focus()
  const open = ours[0]
  if (open) {
    try {
      await open.focus()
      await open.navigate(url)
      return
    } catch {
      // An uncontrolled window can't be navigated — open a fresh one.
    }
  }
  return self.clients.openWindow(url)
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = (event.notification.data && event.notification.data.url) || self.registration.scope
  event.waitUntil(openOrFocus(url))
})
