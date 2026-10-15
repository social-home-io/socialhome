#!/usr/bin/env node
/**
 * fonts:check — CI gate on the BUILT bundles (run after `pnpm run build`).
 *
 * Fails when any shipped file references a third-party font host
 * (fonts.googleapis.com / fonts.gstatic.com …), when a built stylesheet
 * loads a font from an absolute `/…` URL (skips the HA ingress
 * `<base href>` prefix → 404 against HA Core) or a `data:` URI (refused by
 * the CSP's `font-src 'self'`), or when a referenced font file is missing.
 *
 * Scans:
 *   - socialhome/static/                 — the SPA bundle (vite.config.ts)
 *   - socialhome/global_server/static/   — GFS public-page assets + bundles
 *   - socialhome/dashboard/              — the wall-tablet dashboard (port 8100)
 *
 * Source-level twin: client/src/styles/fonts.test.ts (Vitest) and
 * tests/test_no_third_party_fonts.py (pytest).
 *
 * Runs in Node, no deps.
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SPA = join(ROOT, 'socialhome', 'static')
const SCAN = [SPA, join(ROOT, 'socialhome', 'global_server', 'static'), join(ROOT, 'socialhome', 'dashboard')]
const HOST = /fonts\.(googleapis|gstatic)\.com|use\.typekit\.net|fonts\.bunny\.net/i
const TEXT = /\.(html?|css|js|mjs|json|py|svg|txt|webmanifest)$/

function walk(dir, out = []) {
  if (!existsSync(dir)) return out
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) {
      if (name !== '__pycache__') walk(p, out)
    } else out.push(p)
  }
  return out
}

const errors = []

if (!existsSync(join(SPA, 'index.html'))) {
  errors.push(`no SPA build at ${SPA} — run \`pnpm run build\` first`)
}

for (const file of SCAN.flatMap((d) => walk(d))) {
  if (!TEXT.test(file)) continue
  const text = readFileSync(file, 'utf8')
  const rel = file.slice(ROOT.length + 1)
  if (HOST.test(text)) errors.push(`${rel}: references a third-party font host`)
  if (!file.endsWith('.css')) continue
  for (const block of text.match(/@font-face\s*\{[^}]*\}/g) ?? []) {
    for (const [, url] of block.matchAll(/url\(\s*['"]?([^'")]+)['"]?\s*\)/g)) {
      if (url.startsWith('data:')) {
        errors.push(`${rel}: inlines a font as data: (CSP font-src 'self' refuses it)`)
      } else if (/^[a-z]+:|^\/\//i.test(url)) {
        errors.push(`${rel}: loads a font from another origin: ${url}`)
      } else if (url.startsWith('/') && file.startsWith(SPA)) {
        errors.push(`${rel}: absolute font URL ${url} skips the ingress <base href>`)
      } else if (!url.startsWith('/') && !existsSync(resolve(dirname(file), url))) {
        errors.push(`${rel}: font file not found: ${url}`)
      }
    }
  }
}

const index = existsSync(join(SPA, 'index.html')) ? readFileSync(join(SPA, 'index.html'), 'utf8') : ''
for (const [tag] of index.matchAll(/<link[^>]*as="font"[^>]*>/g)) {
  const href = /href="([^"]+)"/.exec(tag)?.[1] ?? ''
  if (!href.startsWith('./assets/')) errors.push(`index.html: font preload ${href} is not a relative ./assets/ URL`)
  else if (!existsSync(join(SPA, href))) errors.push(`index.html: preloaded font ${href} is missing`)
}

if (errors.length) {
  console.error('fonts:check failed:\n  ' + errors.join('\n  '))
  process.exit(1)
}
console.log('fonts:check ok — no third-party font host; every font URL is relative and present')
