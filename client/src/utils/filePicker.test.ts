import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'
import { fileInputMultiple } from './filePicker'

const CHROME = 'Mozilla/5.0 (Linux; Android 16; SM-S931B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36'
// The HA Android app appends " Home Assistant/<VERSION_NAME> (Android …)"
// to the WebView's own user agent (HAWebView.kt, HomeAssistantApis.kt).
const haApp = (version: string) => `${CHROME} Home Assistant/${version} (Android 16; SM-S931B)`

describe('fileInputMultiple', () => {
  it('lets a browser pick many files', () => {
    expect(fileInputMultiple(CHROME)).toBe(true)
  })

  it.each(['2026.10.1-full', '2026.8.4-minimal', '2025.12.0-full', '2026.11.0-full'])(
    'picks one file at a time in the HA Android app %s (it drops multi-picks, home-assistant/android#7548)',
    (version) => {
      expect(fileInputMultiple(haApp(version))).toBe(false)
    },
  )

  it.each(['2026.11.1-full', '2026.11.2-minimal', '2026.12.0-full', '2027.1.0-full'])(
    'picks many files in the fixed HA Android app %s',
    (version) => {
      expect(fileInputMultiple(haApp(version))).toBe(true)
    },
  )

  it('picks one file in an HA Android app whose version it cannot read', () => {
    expect(fileInputMultiple(`${CHROME} Home Assistant/dev (Android 16; Pixel 9)`)).toBe(false)
  })

  it('lets the HA iOS app pick many files (WKWebView never had the bug)', () => {
    const ios = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Home Assistant/2026.10.0 (io.robbie.HomeAssistant; build:2026.1234; iOS 18.0.0)'
    expect(fileInputMultiple(ios)).toBe(true)
  })
})

// Guard: a multi-file picker written as a bare ``multiple`` attribute
// brings back the HA Android "pick photos → nothing happens" bug (#467,
// again after #1275). Every one goes through ``fileInputMultiple()``.
describe('file inputs take ``multiple`` only from fileInputMultiple()', () => {
  const root = join(__dirname, '..')
  const files: string[] = []
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name)
      if (statSync(p).isDirectory()) walk(p)
      else if (p.endsWith('.tsx') && !/\.test\.tsx$/.test(p)) files.push(p)
    }
  }
  walk(root)

  it('has no bare or hard-coded true ``multiple`` attribute', () => {
    const offenders: string[] = []
    for (const f of files) {
      readFileSync(f, 'utf8').split('\n').forEach((line, i) => {
        const code = line.trim()
        if (code.startsWith('*') || code.startsWith('/*') || code.startsWith('//') || code.startsWith('{/*')) return
        // ``multiple`` as a JSX attribute: bare (end of line, before
        // another attribute, a spread or the tag's end), a string
        // (``multiple=""``) or anything but a value from a call / prop
        // (``multiple={true}``, ``multiple={!0}``).
        if (/(^|\s)multiple(\s*$|\s+([\w-]+|\{\.\.\.)|\s*\/?>|=["']|=\{\s*(true|!)\s*)/.test(line)) {
          offenders.push(`${relative(root, f)}:${i + 1}: ${line.trim()}`)
        }
      })
    }
    expect(offenders).toEqual([])
  })
})
