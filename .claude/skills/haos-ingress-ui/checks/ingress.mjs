// haos ingress UI checks — drive the built SPA through the fake Home
// Assistant ingress proxy and assert it never leaves the ingress prefix.
//
// Started by ../harness.py (which boots the stack and passes everything in
// INGRESS_* env vars); not meant to be run by hand. Writes
// $INGRESS_OUT/results.json, requests.json and screenshots/*.png.
//
// Every wait is a wait-for-condition with a timeout — no fixed sleeps, no
// retries. A check that times out fails with the condition it waited for.
import fs from 'node:fs'
import path from 'node:path'
import puppeteer from 'puppeteer-core'

const env = name => {
  const v = process.env[name]
  if (v === undefined) throw new Error(`${name} not set — run via harness.py`)
  return v
}
const PROXY = env('INGRESS_PROXY')
const BACKEND = env('INGRESS_BACKEND')
const SUPERVISOR = env('INGRESS_SUPERVISOR')
const PREFIX = env('INGRESS_PREFIX')
const OUT = env('INGRESS_OUT')
const CHROME = env('INGRESS_CHROME')
const ROUTER_TS = env('INGRESS_ROUTER_TS')
const ONLY = new Set((process.env.INGRESS_ONLY || '').split(',').map(s => s.trim()).filter(Boolean))
const APP = `${PROXY}${PREFIX}/`
const SHOTS = path.join(OUT, 'screenshots')
const BACKEND_LOG = path.join(OUT, 'logs', 'backend.log')
// UI copy comes from the English catalogue so a reworded string doesn't
// break a check that only cares about which screen is up.
const EN = JSON.parse(fs.readFileSync(path.join(path.dirname(ROUTER_TS), 'i18n', 'locales', 'en.json'), 'utf8'))
const T = key => {
  if (!(key in EN)) throw new Error(`i18n key ${key} missing from en.json`)
  return EN[key]
}

const VIEWPORTS = {
  desktop: { width: 1280, height: 720 },
  mobile: { width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
}
const STEP_TIMEOUT = 20_000
const MAX_CONSECUTIVE_ROUTE_FAILURES = 3

const results = []
const browserRequests = []
const consoleLog = []
const pageErrors = []

// ── helpers ────────────────────────────────────────────────────────────

const shotName = s => s.replace(/^\//, '').replace(/[^a-zA-Z0-9-]+/g, '_') || 'root'
const pathOf = page => new URL(page.url()).pathname
const underPrefix = p => p === PREFIX || p.startsWith(PREFIX + '/')

async function proxyCtl(q) {
  const r = await fetch(PROXY + q)
  if (!r.ok) throw new Error(`proxy control ${q} → ${r.status}`)
  return r.json()
}

// Seed through the backend directly, stamping the headers the Supervisor
// would. Seeding is test setup, not the thing under test.
async function asUser(user, method, p, { json, raw } = {}) {
  const headers = { 'X-Hass-Source': 'core.ingress', 'X-Remote-User-Name': user, 'X-Ingress-Path': PREFIX }
  let body
  if (json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(json) }
  if (raw) { headers['Content-Type'] = raw.type; body = raw.data }
  const r = await fetch(BACKEND + p, { method, headers, body })
  const text = await r.text()
  let parsed
  try { parsed = JSON.parse(text) } catch { parsed = text }
  if (r.status >= 400) throw new Error(`seed ${user} ${method} ${p} → ${r.status} ${String(text).slice(0, 200)}`)
  return parsed
}

// 64×48 solid PNG.
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAEAAAAAwCAIAAAAuKetIAAAAPklEQVR4nO3PMQ0AIBDAQMC/58cCYbgsrYJuuzNrlHU2T3K1AAdoAQ7QAhygBThAC3CAFuAALcABWoADtMAHfDkCHu8YcTgAAAAASUVORK5CYII=', 'base64')

async function waitFor(desc, fn, timeout = STEP_TIMEOUT, every = 100) {
  const deadline = Date.now() + timeout
  for (;;) {
    const v = await fn()
    if (v) return v
    if (Date.now() > deadline) throw new Error(`timed out after ${timeout} ms waiting for ${desc}`)
    await new Promise(r => setTimeout(r, every))
  }
}

// Label a wait so a timeout says *what* never happened.
async function step(desc, promise) {
  try { return await promise } catch (e) { throw new Error(`${desc}: ${e.message || e}`) }
}

const backendLog = () => (fs.existsSync(BACKEND_LOG) ? fs.readFileSync(BACKEND_LOG, 'utf8') : '')
const wsConnectedCount = () => (backendLog().match(/ws connected: user=/g) || []).length

let browser
let activePage = null

async function newPage(vp) {
  const ctx = await browser.createBrowserContext()
  const page = await ctx.newPage()
  await page.setViewport(VIEWPORTS[vp])
  page.setDefaultTimeout(STEP_TIMEOUT)
  page.on('request', r => browserRequests.push({
    vp, method: r.method(), url: r.url(), type: r.resourceType(), authorization: 'authorization' in r.headers(),
  }))
  // Idle ledger straight from raw CDP Network events, scoped to the
  // *current* document of each frame. Not ``page.waitForNetworkIdle``:
  // right after the haos setup, ``loadInstanceConfig()`` fires the App's
  // ``/api/me`` probe in the same tick as ``location.href = basePath``;
  // that fetch dies with the old document and Chrome intermittently
  // never reports it finished or failed — puppeteer's page-wide counter
  // (and a naive CDP ledger) then hold it forever and every later wait
  // on the page times out. A request from a replaced document can't
  // affect the page any more, so it's dropped from the ledger.
  const inflight = new Map()
  const loaders = new Map() // frameId → loaderId of its current document
  page.inflight = inflight
  page.lastActivity = Date.now()
  const cdp = await page.createCDPSession()
  cdp.on('Network.requestWillBeSent', e => {
    page.lastActivity = Date.now()
    const current = loaders.get(e.frameId)
    if (e.type !== 'Document' && current !== undefined && e.loaderId !== current) return
    inflight.set(e.requestId, { method: e.request.method, url: e.request.url, type: e.type, since: Date.now(), frameId: e.frameId, loaderId: e.loaderId })
  })
  const done = e => { inflight.delete(e.requestId); page.lastActivity = Date.now() }
  cdp.on('Network.loadingFinished', done)
  cdp.on('Network.loadingFailed', done)
  cdp.on('Page.frameNavigated', ({ frame }) => {
    loaders.set(frame.id, frame.loaderId)
    for (const [id, r] of inflight) {
      if (r.frameId === frame.id && r.loaderId !== frame.loaderId) inflight.delete(id)
    }
  })
  await cdp.send('Network.enable')
  await cdp.send('Page.enable')
  page.on('console', m => {
    if (['error', 'warn'].includes(m.type())) consoleLog.push(`[${vp}] ${m.type()}: ${m.text()}`.slice(0, 400))
  })
  page.on('pageerror', e => pageErrors.push({ vp, url: page.url(), error: String(e).slice(0, 400) }))
  activePage = page
  return { ctx, page }
}

// The SPA has painted *a* terminal screen: the app shell, the haos
// setup wizard, IngressAuthFailed or the error boundary — then the
// network has gone quiet (lazy route chunks + first API calls done).
async function settled(page) {
  await step(
    'the SPA to paint a screen (app shell, setup wizard or sign-in state)',
    page.waitForFunction(() => !!document.querySelector('#main, .sh-setup-title, .sh-login'), { timeout: STEP_TIMEOUT }),
  )
  await networkIdle(page)
}

// No request in flight for ``idleMs`` (lazy route chunks + first API
// calls done). The realtime WebSocket doesn't count.
async function networkIdle(page, idleMs = 300) {
  try {
    await waitFor('the network to go idle', () => page.inflight.size === 0 && Date.now() - page.lastActivity >= idleMs, STEP_TIMEOUT, 50)
  } catch (e) {
    const stuck = [...page.inflight.values()].map(r => `${r.method} ${r.url} [${r.type}, ${Date.now() - r.since} ms]`)
    throw new Error(`${e.message}; still in flight: ${stuck.join(', ')}`)

  }
}

// ``GET /api/x ×3`` — collapse repeats so the report stays readable.
function tally(lines) {
  const counts = new Map()
  for (const l of lines) counts.set(l, (counts.get(l) || 0) + 1)
  return [...counts].map(([l, n]) => (n > 1 ? `${l} ×${n}` : l))
}

async function clickButton(page, label) {
  await page.waitForFunction(t => [...document.querySelectorAll('button')].some(b => b.textContent.trim() === t), {}, label)
  await page.evaluate(t => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === t).click(), label)
}

async function screenState(page) {
  return page.evaluate((authFailed, crashed) => ({
    baseURI: document.baseURI,
    authFailed: document.body.innerText.includes(authFailed),
    crashed: document.body.innerText.includes(crashed),
    hasMain: !!document.querySelector('#main'),
  }), T('ingress_failed.title'), T('error_boundary.title'))
}

async function check(name, fn, { browser = true } = {}) {
  if (ONLY.size && !ONLY.has(name) && !name.startsWith('setup') && !name.startsWith('request-audit')) return
  const errorsBefore = pageErrors.length
  const violationsBefore = (await proxyCtl('/_proxy/log')).filter(e => e.violation).length
  // Name the most likely cause first: a request that escaped the prefix
  // during this check is why a screen never painted.
  const escaped = async () => {
    if (!browser) return []
    const v = (await proxyCtl('/_proxy/log')).filter(e => e.violation).slice(violationsBefore)
    return v.length ? [`${v.length} request(s) escaped the ingress prefix during this check: ${tally(v.map(e => `${e.method} ${e.path}`)).slice(0, 5).join(', ')}`] : []
  }
  let r
  try {
    const d = (await fn()) || {}
    const newErrors = pageErrors.slice(errorsBefore)
    const problems = [...(await escaped()), ...(d.problems || [])]
    if (newErrors.length) problems.push(...newErrors.map(e => `uncaught page error on ${e.url}: ${e.error}`))
    r = { name, ok: problems.length === 0, detail: { ...d, problems } }
  } catch (e) {
    r = { name, ok: false, detail: { problems: [...(await escaped()), String(e.message || e)], pageErrors: pageErrors.slice(errorsBefore) } }
  }
  if (!r.ok && activePage && !activePage.isClosed()) {
    try { await activePage.screenshot({ path: path.join(SHOTS, `FAIL-${name}.png`) }) } catch { /* page gone */ }
  }
  results.push(r)
  console.log(`${r.ok ? 'PASS' : 'FAIL'} ${name}${r.ok ? '' : '\n  ' + r.detail.problems.join('\n  ')}`)
}

// Routes straight from the SPA's route table, so a new page is walked
// the day it lands. ``:id`` params are filled from seeded content; a
// route whose param we can't fill is skipped (and listed in the report).
function routeTable(ids) {
  const src = fs.readFileSync(ROUTER_TS, 'utf8')
  const keys = [...src.matchAll(/^\s*'(\/[^']*)'\s*:/gm)].map(m => m[1])
  const fill = { '/spaces/:id': ids.spaceId, '/dms/:id': ids.dmId }
  const walk = []
  const skipped = []
  for (const k of keys) {
    let r = k
    for (const [stem, val] of Object.entries(fill)) {
      if (r.startsWith(stem)) r = r.replace(':id', val)
    }
    if (r.includes(':')) skipped.push(k)
    else walk.push(r)
  }
  walk.push('/search?q=hello')
  return { walk, skipped }
}

// ── run ────────────────────────────────────────────────────────────────

fs.mkdirSync(SHOTS, { recursive: true })
await proxyCtl('/_proxy/user?name=alice')
await proxyCtl('/_proxy/reset')
browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  protocolTimeout: 120_000,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--hide-scrollbars', '--lang=en-US'],
  env: { ...process.env, LANG: 'en_US.UTF-8', LANGUAGE: 'en_US' },
})

const { ctx: mainCtx, page } = await newPage('desktop')
const ids = {}

// 1. Fresh instance → the haos setup wizard through ingress. No token
//    anywhere: the complete endpoint returns {username}, the SPA stores
//    nothing, and lands in the app shell under the prefix.
await check('setup-haos-flow', async () => {
  const problems = []
  await page.goto(APP, { waitUntil: 'load' })
  await step('setup wizard paints', settled(page))
  await page.screenshot({ path: path.join(SHOTS, 'setup-1-welcome.png') })
  await step(`welcome CTA "${T('setup.haos.continue')}"`, clickButton(page, T('setup.haos.continue')))
  const input = await step('household-name field', page.waitForSelector('input[name=household_name]'))
  await input.type('Ingress House')
  await page.screenshot({ path: path.join(SHOTS, 'setup-2-name.png') })
  const [resp] = await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/setup/haos/complete') && r.request().method() === 'POST'),
    page.click('form.sh-setup-form button[type=submit]'),
  ])
  const body = await resp.json().catch(() => null)
  if (!underPrefix(new URL(resp.url()).pathname)) problems.push(`setup POST went to ${resp.url()} (outside the prefix)`)
  if (resp.status() !== 200) problems.push(`setup POST → ${resp.status()}`)
  if (body && 'token' in body) problems.push('POST /api/setup/haos/complete returned a token — haos must not')
  // ``window.location.href = basePath`` reloads into the app; a brand-new
  // member gets the onboarding tour first — skip it (its "done" call
  // goes through the api client, so it is under test too).
  await step('the onboarding tour after setup', clickButton(page, T('onboarding.skip')))
  await step('app shell after the tour', page.waitForSelector('#main'))
  await settled(page)
  const stored = await page.evaluate(() => localStorage.getItem('sh_token'))
  if (stored !== null) problems.push(`localStorage sh_token = ${stored} — haos SPA must never hold a token`)
  if (!underPrefix(pathOf(page))) problems.push(`landed on ${pathOf(page)}, outside the prefix`)
  await page.screenshot({ path: path.join(SHOTS, 'setup-3-app.png') })
  return { problems, setupResponse: body, landedOn: pathOf(page) }
})

// Seed content now that alice (the HA owner) is the admin; provision
// bob + carol from the Home Assistant user list.
await check('setup-seed-content', async () => {
  await asUser('alice', 'POST', '/api/me/onboarding-complete')
  for (const u of ['bob', 'carol']) {
    await asUser('alice', 'POST', `/api/admin/ha-users/${u}/provision`, { json: {} })
    await asUser(u, 'POST', '/api/me/onboarding-complete')
  }
  await asUser('alice', 'POST', '/api/feed/posts', { json: { type: 'text', content: 'Hello through the ingress proxy' } })
  const boundary = '----ingress'
  const mp = Buffer.concat([
    Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\nContent-Type: image/png\r\n\r\n`),
    PNG, Buffer.from(`\r\n--${boundary}--\r\n`),
  ])
  const up = await asUser('alice', 'POST', '/api/media/upload', { raw: { type: `multipart/form-data; boundary=${boundary}`, data: mp } })
  await asUser('alice', 'POST', '/api/feed/posts', { json: { type: 'image', content: 'Image via ingress', image_urls: [up.url] } })
  const dm = await asUser('alice', 'POST', '/api/conversations/dm', { json: { username: 'bob' } })
  ids.dmId = dm.id || dm.conversation_id
  await asUser('bob', 'POST', `/api/conversations/${ids.dmId}/messages`, { json: { content: 'Hi from Bob' } })
  const sp = await asUser('alice', 'POST', '/api/spaces', { json: { name: 'Ingress Space', emoji: '🚪' } })
  ids.spaceId = sp.id || sp.space_id
  await asUser('alice', 'POST', `/api/spaces/${ids.spaceId}/posts`, { json: { type: 'text', content: 'Space post via ingress' } })
  const album = await asUser('alice', 'POST', '/api/gallery/albums', { json: { name: 'Ingress album' } })
  ids.albumId = album.id || album.album_id
  const missing = Object.entries(ids).filter(([, v]) => !v).map(([k]) => `seed returned no ${k}`)
  return { problems: missing, ids }
}, { browser: false })

// 2. Every route in the SPA's route table, loaded cold under the prefix,
//    on desktop and mobile: base href, location, auth and render intact.
for (const vp of ['desktop', 'mobile']) {
  await check(`walk-routes-${vp}`, async () => {
    const { ctx, page: p } = vp === 'desktop' ? { ctx: null, page } : await newPage('mobile')
    const { walk, skipped } = routeTable(ids)
    const problems = []
    let consecutive = 0
    for (const r of walk) {
      // One broken route must not hide the next: record and move on —
      // but when the app can't render anything, every route would wait
      // out its timeout; stop after a few in a row.
      if (consecutive >= MAX_CONSECUTIVE_ROUTE_FAILURES) {
        problems.push(`walk aborted after ${consecutive} routes in a row failed to render — fix the first failure above`)
        break
      }
      try {
        await p.goto(PROXY + PREFIX + r, { waitUntil: 'load' })
        await step(`${r} renders`, settled(p))
      } catch (e) {
        problems.push(String(e.message || e))
        await p.screenshot({ path: path.join(SHOTS, `FAIL-walk-${vp}-${shotName(r)}.png`) })
        consecutive += 1
        continue
      }
      consecutive = 0
      const s = await screenState(p)
      if (!underPrefix(new URL(s.baseURI).pathname)) problems.push(`${r}: document.baseURI is ${s.baseURI}`)
      if (!underPrefix(pathOf(p))) problems.push(`${r}: location left the prefix → ${pathOf(p)}`)
      if (s.authFailed) problems.push(`${r}: IngressAuthFailed shown (ingress auth lost)`)
      if (s.crashed) problems.push(`${r}: error boundary ("${T('error_boundary.title')}")`)
      if (!s.hasMain) problems.push(`${r}: app shell (#main) missing`)
      await p.screenshot({ path: path.join(SHOTS, `walk-${vp}-${shotName(r)}.png`) })
    }
    if (ctx) await ctx.close()
    activePage = page
    return { problems, walked: walk.length, skipped }
  })
}

// 3. The realtime WebSocket opens under the prefix (through the proxy's
//    WS upgrade) and a post by someone else shows up live.
await check('ws-under-prefix-and-live', async () => {
  const problems = []
  const before = wsConnectedCount()
  await page.goto(APP + 'feed', { waitUntil: 'load' })
  await settled(page)
  // The backend logs ``ws connected`` once the socket is registered —
  // only then can a broadcast reach this page.
  await waitFor('the SPA WebSocket to register with the backend', () => wsConnectedCount() > before)
  const text = `INGRESS-LIVE-${Date.now().toString(36)}`
  await asUser('bob', 'POST', '/api/feed/posts', { json: { type: 'text', content: text } })
  try {
    await page.waitForFunction(t => document.body.innerText.includes(t), {}, text)
  } catch {
    problems.push(`bob's post "${text}" never appeared live on alice's feed`)
  }
  const log = await proxyCtl('/_proxy/log')
  const ws = log.filter(e => e.ws)
  if (!ws.length) problems.push('no WebSocket upgrade reached the ingress proxy')
  for (const e of ws) {
    if (!e.path.startsWith(PREFIX + '/api/ws')) problems.push(`WebSocket opened at ${e.path}, not ${PREFIX}/api/ws`)
    if (e.status !== 101) problems.push(`WebSocket ${e.path} → ${e.status}`)
  }
  return { problems, wsPaths: [...new Set(ws.map(e => e.path.split('?')[0]))] }
})

// 3b. Cold load of a *nested* route (``<prefix>/dms/<id>``): the socket
//     still opens at ``<prefix>/api/ws`` and a DM arrives live. The SPA
//     must hand ``new WebSocket`` an absolute URL — Firefox resolves a
//     relative one against the page URL (``<prefix>/dms/api/ws``),
//     ignoring ``<base href>``. Chrome honours the base, so the raw
//     constructor argument is recorded and asserted directly: a
//     relative URL fails here even though Chrome would have connected.
await check('ws-nested-route-cold-load', async () => {
  const problems = []
  const { ctx, page: p } = await newPage('desktop')
  try {
    await p.evaluateOnNewDocument(() => {
      const Native = window.WebSocket
      window.__shWsArgs = []
      window.WebSocket = class extends Native {
        constructor(url, protocols) {
          window.__shWsArgs.push(String(url))
          super(url, protocols)
        }
      }
    })
    const logBefore = (await proxyCtl('/_proxy/log')).length
    const before = wsConnectedCount()
    await p.goto(APP + `dms/${ids.dmId}`, { waitUntil: 'load' })
    await settled(p)
    await waitFor('the SPA WebSocket to register with the backend', () => wsConnectedCount() > before)
    const args = await p.evaluate(() => window.__shWsArgs)
    if (!args.length) problems.push('the SPA never constructed a WebSocket')
    for (const a of args) {
      if (!/^wss?:\/\//.test(a)) problems.push(`new WebSocket(${JSON.stringify(a.split('?')[0])}) got a relative URL — Firefox resolves it against the page, not <base href>`)
      else if (!new URL(a).pathname.startsWith(PREFIX + '/api/')) problems.push(`new WebSocket(${a.split('?')[0]}) is outside ${PREFIX}/api/`)
    }
    const text = `INGRESS-NESTED-${Date.now().toString(36)}`
    await asUser('bob', 'POST', `/api/conversations/${ids.dmId}/messages`, { json: { content: text } })
    try {
      await p.waitForFunction(t => document.body.innerText.includes(t), {}, text)
    } catch {
      problems.push(`bob's DM "${text}" never appeared live on alice's cold-loaded thread`)
    }
    const ws = (await proxyCtl('/_proxy/log')).slice(logBefore).filter(e => e.ws)
    if (!ws.length) problems.push('no WebSocket upgrade reached the ingress proxy')
    for (const e of ws) {
      if (!e.path.startsWith(PREFIX + '/api/ws')) problems.push(`WebSocket opened at ${e.path}, not ${PREFIX}/api/ws`)
    }
    await p.screenshot({ path: path.join(SHOTS, problems.length ? 'FAIL-ws-nested-route-cold-load.png' : 'ws-nested-route-cold-load.png') })
    return { problems, wsArgs: args.map(a => a.split('?')[0]) }
  } finally {
    await ctx.close()
    activePage = page
  }
})

// 4. In-app navigation, reload, back and forward keep the prefix.
await check('router-keeps-prefix', async () => {
  await page.goto(APP, { waitUntil: 'load' })
  await settled(page)
  const seen = []
  const go = async suffix => {
    const clicked = await page.evaluate(s => {
      const a = [...document.querySelectorAll('a[href]')].find(el => new URL(el.href).pathname.endsWith(s) && el.offsetParent !== null)
      a?.click()
      return !!a
    }, suffix)
    if (!clicked) throw new Error(`no visible nav link ending in ${suffix}`)
    await step(`navigation to ${PREFIX + suffix}`, page.waitForFunction(want => location.pathname === want, {}, PREFIX + suffix))
    await settled(page)
    seen.push(pathOf(page))
  }
  const history = async (fn, want) => {
    await fn()
    await step(`history step to ${PREFIX + want} (at ${pathOf(page)})`, page.waitForFunction(w => location.pathname === w, {}, PREFIX + want))
    await settled(page)
    seen.push(pathOf(page))
  }
  await go('/spaces')
  await go('/calendar')
  await history(() => page.reload({ waitUntil: 'load' }), '/calendar')
  await history(() => page.goBack(), '/spaces')
  await history(() => page.goBack(), '/')
  await history(() => page.goForward(), '/spaces')
  // Momentum's tabs are links too (/momentum, /momentum/yours): following
  // one keeps the prefix, and Back returns to the other tab.
  await go('/momentum')
  await go('/momentum/yours')
  await history(() => page.goBack(), '/momentum')
  return { problems: [], seen }
})

// 5. Media: feed images load through the prefix; a gallery upload (raw
//    XHR, not the api client) posts under the prefix and renders.
await check('media-load-and-upload', async () => {
  const problems = []
  await page.goto(APP + 'feed', { waitUntil: 'load' })
  await settled(page)
  await step('a feed image to load', page.waitForFunction(() => [...document.querySelectorAll('#main img')].some(i => /media/.test(i.src) && i.complete && i.naturalWidth > 0)))
  const imgs = await page.$$eval('#main img', is => is.filter(i => /media/.test(i.src)).map(i => ({ src: i.src, loaded: i.complete && i.naturalWidth > 0 })))
  for (const i of imgs) {
    if (!underPrefix(new URL(i.src).pathname)) problems.push(`feed image outside the prefix: ${i.src}`)
    if (!i.loaded) problems.push(`feed image did not load: ${i.src}`)
  }
  await page.goto(APP + 'gallery', { waitUntil: 'load' })
  await settled(page)
  await step('the seeded album card', page.waitForFunction(() => [...document.querySelectorAll('a[href], button')].some(e => e.textContent.includes('Ingress album'))))
  await page.evaluate(() => [...document.querySelectorAll('a[href], button')].find(e => e.textContent.includes('Ingress album')).click())
  const fileInput = await step('the album upload input', page.waitForSelector('input[type=file]'))
  const file = path.join(OUT, 'upload.png')
  fs.writeFileSync(file, PNG)
  const [resp] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'POST' && /api\/gallery\/albums\/[^/]+\/items$/.test(new URL(r.url()).pathname)),
    fileInput.uploadFile(file),
  ])
  if (!underPrefix(new URL(resp.url()).pathname)) problems.push(`gallery upload POSTed outside the prefix: ${resp.url()}`)
  if (resp.status() >= 300) problems.push(`gallery upload → ${resp.status()}`)
  await settled(page)
  await page.screenshot({ path: path.join(SHOTS, 'media-gallery-after-upload.png') })
  return { problems, feedImages: imgs, upload: { url: resp.url(), status: resp.status() } }
})

// 6. The way Home Assistant really mounts the panel: inside an iframe.
await check('iframe-embed', async () => {
  const { ctx, page: p } = await newPage('desktop')
  await p.goto(PROXY + '/frame', { waitUntil: 'load' })
  const handle = await p.waitForSelector('iframe#app')
  const frame = await handle.contentFrame()
  await step('the app shell inside the iframe', frame.waitForSelector('#main'))
  await frame.evaluate(() => {
    const a = [...document.querySelectorAll('a[href]')].find(el => new URL(el.href).pathname.endsWith('/dms') && el.offsetParent !== null)
    if (!a) throw new Error('no visible /dms link inside the iframe')
    a.click()
  })
  await frame.waitForFunction(want => location.pathname === want, {}, PREFIX + '/dms')
  const topPath = pathOf(p)
  await p.screenshot({ path: path.join(SHOTS, 'iframe-dms.png') })
  await ctx.close()
  activePage = page
  return { problems: topPath === '/frame' ? [] : [`the top frame navigated to ${topPath}`] }
})

// 7. No ingress user (Supervisor sent no X-Remote-User-*): the cold-start
//    /api/me 401s → IngressAuthFailed, never a password form or a
//    "Session expired" toast (there was no token to expire).
await check('ingress-401-auth-failed', async () => {
  await proxyCtl('/_proxy/user?name=none')
  try {
    const { ctx, page: p } = await newPage('desktop')
    await p.goto(APP, { waitUntil: 'load' })
    await step('IngressAuthFailed screen', p.waitForFunction(t => document.body.innerText.includes(t), {}, T('ingress_failed.title')))
    await networkIdle(p)
    const state = await p.evaluate(() => ({
      passwordField: !!document.querySelector('input[type=password]'),
      toasts: [...document.querySelectorAll('.sh-toast-message')].map(e => e.textContent),
    }))
    await p.screenshot({ path: path.join(SHOTS, 'ingress-auth-failed.png') })
    await ctx.close()
    activePage = page
    const problems = []
    if (state.passwordField) problems.push('a password field is shown — haos must never ask for one')
    if (state.toasts.length) problems.push(`toast(s) shown on an ingress 401: ${state.toasts.join(' | ')}`)
    return { problems, ...state }
  } finally {
    await proxyCtl('/_proxy/user?name=alice')
  }
})

await mainCtx.close()
await browser.close()

// 8. Request audit over everything the browser sent in every check.
await check('request-audit', async () => {
  const proxyLog = await proxyCtl('/_proxy/log')
  const http = browserRequests.filter(r => /^(https?|wss?):/.test(r.url))
  const outside = http.filter(r => new URL(r.url).origin === PROXY && !underPrefix(new URL(r.url).pathname) && new URL(r.url).pathname !== '/frame')
  const foreign = http.filter(r => new URL(r.url).origin !== PROXY)
  const withAuth = http.filter(r => r.authorization)
  const violations = proxyLog.filter(e => e.violation)
  const serverErrors = proxyLog.filter(e => e.status >= 500)
  const problems = tally([
    ...violations.map(e => `proxy saw ${e.method} ${e.path} OUTSIDE ${PREFIX} (a URL that ignores <base href>)`),
    ...outside.filter(r => !violations.some(v => PROXY + v.path === r.url)).map(r => `browser requested ${r.method} ${r.url} outside the prefix`),
    ...foreign.map(r => `browser requested a foreign origin: ${r.method} ${r.url}`),
    ...withAuth.map(r => `browser sent an Authorization header (haos has no token): ${r.method} ${r.url}`),
    ...serverErrors.map(e => `${e.method} ${e.path} → ${e.status}`),
  ])
  return {
    problems,
    browserRequests: http.length,
    proxyRequests: proxyLog.length,
    proxyStatus: proxyLog.reduce((a, e) => ((a[e.status] = (a[e.status] || 0) + 1), a), {}),
    wsPaths: [...new Set(proxyLog.filter(e => e.ws).map(e => e.path.split('?')[0]))],
  }
}, { browser: false })

// 9. The backend really talked to "Home Assistant": Supervisor info, the
//    HA user list over the Core WebSocket, all with a valid token.
await check('supervisor-contract', async () => {
  const calls = await (await fetch(`${SUPERVISOR}/_log`)).json()
  const has = (kind, what) => calls.some(c => c.kind === kind && c.what === what && (c.status === 200 || c.status === 'ok'))
  const problems = []
  if (!has('supervisor', 'GET /addons/self/info')) problems.push('backend never read GET /addons/self/info')
  if (!has('ws', 'config/auth/list')) problems.push('backend never listed HA users over the Core WebSocket (config/auth/list)')
  for (const c of calls) {
    if ([401, 403, 'auth_invalid'].includes(c.status)) problems.push(`${c.kind} ${c.what} rejected the Supervisor token (${c.status})`)
  }
  return { problems, unmocked: calls.filter(c => c.unmocked), calls: calls.length }
}, { browser: false })

fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({ results, console: consoleLog, pageErrors }, null, 2))
fs.writeFileSync(path.join(OUT, 'requests.json'), JSON.stringify({ browser: browserRequests, proxy: await proxyCtl('/_proxy/log') }, null, 2))
process.exit(results.every(r => r.ok) ? 0 : 1)
