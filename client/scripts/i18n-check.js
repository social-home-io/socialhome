#!/usr/bin/env node
/**
 * i18n:check — PR gate for the Weblate-backed translation workflow
 * (spec §30.5). Fails the build when any `t("…")` / `t('…')` call
 * in `src/` references a key that isn't in `src/i18n/locales/en.json`,
 * and warns when `en.json` contains keys with no call site (dead
 * strings that shouldn't be pushed to translators).
 *
 * Missing-from-en → HARD FAIL (exit 1). This is the PR gate.
 * Unused-in-en    → WARNING (exit 0). Useful signal but not fatal —
 *                   some keys are built dynamically (e.g.
 *                   `t(\`presence.state.${state}\`)`) and can't be
 *                   statically detected.
 *
 * It also checks every shipped locale (`_meta.json`) against en.json:
 *
 * Untranslated key → HARD FAIL, unless the key is listed for that locale
 *                    in `scripts/i18n-untranslated.json` (the allow-list
 *                    for strings deliberately left to Weblate). Keeps
 *                    English fallbacks from piling up unseen.
 * Placeholder drift → HARD FAIL. A translation must carry exactly the
 *                    `{param}` names of its English source, or the
 *                    value never gets substituted.
 * Orphan key       → HARD FAIL. A locale key with no en.json source is
 *                    dead weight nobody can see.
 *
 * Runs in Node, no deps beyond what ships with the client bundle.
 */
import { readFileSync } from 'node:fs'
import { readdirSync, statSync } from 'node:fs'
import { join, extname } from 'node:path'

const ROOT = new URL('..', import.meta.url).pathname
const SRC_DIR = join(ROOT, 'src')
const LOCALES_DIR = join(SRC_DIR, 'i18n', 'locales')
const EN_FILE = join(LOCALES_DIR, 'en.json')
const META_FILE = join(LOCALES_DIR, '_meta.json')
const ALLOW_FILE = join(ROOT, 'scripts', 'i18n-untranslated.json')

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    const st = statSync(full)
    if (st.isDirectory()) {
      // Skip node_modules, dist, .vite, generated catalogs.
      if (entry === 'node_modules' || entry === 'dist' || entry === '.vite'
          || entry === 'locales') continue
      yield* walk(full)
    } else if (/\.(ts|tsx|js|jsx)$/.test(extname(full))) {
      // Skip tests — they deliberately reference missing keys to
      // verify fallback behaviour.
      if (/\.test\.(ts|tsx|js|jsx)$/.test(full)) continue
      yield full
    }
  }
}

// Match t("key") / t('key') — ignore template literals (dynamic
// composition). The pattern intentionally excludes ${…} keys so a
// false positive on `t(\`presence.state.${s}\`)` doesn't break CI.
const T_CALL = /\bt\(\s*['"]([A-Za-z0-9_.-]+)['"]/g

const en = JSON.parse(readFileSync(EN_FILE, 'utf8'))
const enKeys = new Set(Object.keys(en))

const used = new Set()
const offenders = [] // {file, key, line}

for (const file of walk(SRC_DIR)) {
  const src = readFileSync(file, 'utf8')
  T_CALL.lastIndex = 0
  let m
  while ((m = T_CALL.exec(src))) {
    const key = m[1]
    used.add(key)
    if (!enKeys.has(key)) {
      // Compute line for a clean error message.
      const upto = src.slice(0, m.index)
      const line = upto.split('\n').length
      offenders.push({ file, key, line })
    }
  }
}

let failed = false
if (offenders.length) {
  console.error(`✗ i18n:check — ${offenders.length} missing key(s) in en.json:`)
  for (const o of offenders) {
    const rel = o.file.slice(ROOT.length)
    console.error(`  ${rel}:${o.line}  →  ${o.key}`)
  }
  console.error('\nAdd the missing keys to src/i18n/locales/en.json and try again.')
  console.error('Weblate will pick the new keys up on the next poll (≤ 5 min).')
  failed = true
}

// Dead-string warnings — helpful but non-fatal (see preamble).
const unused = [...enKeys].filter((k) => !used.has(k))
if (unused.length) {
  const cap = 20
  console.warn(`⚠ ${unused.length} key(s) in en.json have no matching t() call site:`)
  for (const k of unused.slice(0, cap)) console.warn(`  ${k}`)
  if (unused.length > cap) console.warn(`  … and ${unused.length - cap} more`)
  console.warn('If these are truly unused, remove them from en.json so translators don\'t waste cycles.')
  console.warn('If they are looked up dynamically, add a comment in src/ listing them so future greps hit.')
}

// ── Locale catalogs vs en.json ────────────────────────────────────
const PLACEHOLDER = /\{(\w+)\}/g
const paramSet = (s) => new Set([...s.matchAll(PLACEHOLDER)].map((m) => m[1]))
const params = (s) => [...paramSet(s)].sort().join(',')
// Exact match, except a `_one` plural may use the count its English
// source spells out ("1 person …" vs French "{n} personne …", since 0 is
// "one" there): it may add placeholders from its base key, and drop them.
function placeholdersMatch(key, enText, text) {
  if (params(enText) === params(text)) return true
  if (!key.endsWith('_one')) return false
  const base = en[key.slice(0, -4)]
  if (base === undefined) return false
  const own = paramSet(enText)
  const allowed = new Set([...own, ...paramSet(base)])
  const got = paramSet(text)
  const baseOnly = paramSet(base)
  return [...got].every((p) => allowed.has(p))
    && [...own].every((p) => got.has(p) || baseOnly.has(p))
}
const meta = JSON.parse(readFileSync(META_FILE, 'utf8'))
let allow = {}
try { allow = JSON.parse(readFileSync(ALLOW_FILE, 'utf8')) } catch { /* no allow-list */ }

const summary = []
for (const [lang, info] of Object.entries(meta.locales)) {
  if (info.source) continue
  const cat = JSON.parse(readFileSync(join(LOCALES_DIR, `${lang}.json`), 'utf8'))
  const allowed = new Set(allow[lang] ?? [])
  const untranslated = [...enKeys].filter((k) => !(k in cat) && !allowed.has(k))
  const orphans = Object.keys(cat).filter((k) => !enKeys.has(k))
  const drift = [...enKeys].filter((k) => k in cat && !placeholdersMatch(k, en[k], cat[k]))
  const report = (label, keys, show) => {
    if (!keys.length) return
    failed = true
    console.error(`✗ i18n:check — ${lang}.json: ${keys.length} ${label}:`)
    for (const k of keys.slice(0, 20)) console.error(`  ${show(k)}`)
    if (keys.length > 20) console.error(`  … and ${keys.length - 20} more`)
  }
  report('untranslated key(s)', untranslated, (k) => k)
  report('key(s) with no en.json source', orphans, (k) => k)
  report('key(s) whose {placeholders} differ from en', drift,
    (k) => `${k}: en {${params(en[k])}} vs ${lang} {${params(cat[k])}}`)
  summary.push(`${lang} ${enKeys.size - untranslated.length - allowed.size}/${enKeys.size}`
    + (allowed.size ? ` (+${allowed.size} allow-listed)` : ''))
}
if (failed && summary.length) {
  console.error('\nTranslate the keys, or list them per locale in scripts/i18n-untranslated.json')
  console.error('when they are deliberately left for Weblate.')
}

if (!failed) {
  const count = enKeys.size
  console.log(`✓ i18n:check — ${count} keys, no missing strings. Locales: ${summary.join(', ')}.`)
}

process.exit(failed ? 1 : 0)
