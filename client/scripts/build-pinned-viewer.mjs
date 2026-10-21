#!/usr/bin/env node
/* Build the PINNED public-highlight viewer (§highlights_public).
 *
 *   pnpm build:pinned-viewer
 *
 * Writes
 *   client/dist-pinned/v/<hash>/index.html   static page, strict CSP meta
 *   client/dist-pinned/v/<hash>/viewer.js    the one self-contained module
 *   socialhome/pinned_viewer.json            {version, path, integrity} — COMMITTED
 *
 * ``viewer.js`` is pinned in the page by its sha384 (SRI ``integrity`` and
 * the CSP ``script-src``). ``<hash>`` is the first 16 hex chars of the
 * sha384 of ``index.html``, which carries that integrity and the inline
 * styles, so the path covers every byte served under it: a path never
 * changes content, and older versions stay up next to newer ones.
 *
 * Reproducible: the same lockfile gives the same bytes. No timestamps; the
 * page is assembled from fixed strings. CI rebuilds and fails when the
 * committed manifest differs (``.github/workflows/ci.yml``).
 */
import { createHash } from 'node:crypto'
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'

const CLIENT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const OUT = join(CLIENT, 'dist-pinned')
const STAGE = join(OUT, '.build')
const MANIFEST = resolve(CLIENT, '..', 'socialhome', 'pinned_viewer.json')
const CSS_SOURCE = join(CLIENT, 'gfs', 'public_highlight.css')

/** The stylesheet as the page inlines it: comments dropped, one rule per
 *  line joined without line breaks — byte-identical to the GFS page's
 *  ``_VIEWER_CSS`` (a pytest checks). */
function inlineCss(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .map((line) => line.trim())
    .join('')
}

function digest(algo, bytes) {
  return createHash(algo).update(bytes).digest()
}

/** The page. Everything it loads is ``viewer.js`` next to it (by SRI) and
 *  the inline styles (by hash); requests go only to the GFS the link
 *  names (``connect-src https:``; http only for the localhost dev / demo
 *  setup). ``'self'`` beside the script hash is for browsers that don't
 *  match a hash against an external script; the tag's ``integrity`` pins
 *  the bytes either way. */
function pageHtml({ css, scriptIntegrity }) {
  const styleHash = `sha256-${digest('sha256', Buffer.from(css, 'utf8')).toString('base64')}`
  const csp = [
    "default-src 'none'",
    `script-src 'self' '${scriptIntegrity}'`,
    `style-src '${styleHash}'`,
    'img-src blob: data:',
    'media-src blob:',
    'connect-src https: http://localhost:* http://127.0.0.1:*',
    "base-uri 'none'",
    "form-action 'none'",
  ].join('; ')
  return [
    '<!doctype html>',
    '<html lang="en">',
    '<head>',
    '<meta charset="utf-8">',
    `<meta http-equiv="Content-Security-Policy" content="${csp}">`,
    '<meta name="referrer" content="no-referrer">',
    '<meta name="robots" content="noindex, nofollow">',
    '<meta name="viewport" content="width=device-width,initial-scale=1">',
    '<title>Highlight</title>',
    '<link rel="icon" href="data:,">',
    `<style>${css}</style>`,
    '</head>',
    '<body>',
    '<div id="root"></div>',
    `<script type="module" src="viewer.js" integrity="${scriptIntegrity}" crossorigin="anonymous"></script>`,
    '</body>',
    '</html>',
    '',
  ].join('\n')
}

async function main() {
  await rm(STAGE, { recursive: true, force: true })
  await build({ configFile: join(CLIENT, 'vite.pinned.config.ts'), root: CLIENT })
  const script = await readFile(join(STAGE, 'viewer.js'))
  const scriptIntegrity = `sha384-${digest('sha384', script).toString('base64')}`
  const css = inlineCss(await readFile(CSS_SOURCE, 'utf8'))
  const html = pageHtml({ css, scriptIntegrity })
  const hash = digest('sha384', Buffer.from(html, 'utf8')).toString('hex').slice(0, 16)
  const dir = join(OUT, 'v', hash)
  await mkdir(dir, { recursive: true })
  await writeFile(join(dir, 'viewer.js'), script)
  await writeFile(join(dir, 'index.html'), html)
  await rm(STAGE, { recursive: true, force: true })
  const manifest = { version: 1, path: `v/${hash}/`, integrity: scriptIntegrity }
  await writeFile(MANIFEST, JSON.stringify(manifest, null, 2) + '\n')
  console.log(`pinned viewer: dist-pinned/${manifest.path} (${scriptIntegrity})`)
}

await main()
