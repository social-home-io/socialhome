import { afterEach, describe, expect, it, vi } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { haLogoFile, haLogoUrl } from './haLogo'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const SRC = join(__dirname, '..', '..')
const ASSET_DIR = join(SRC, 'assets', 'home-assistant')

afterEach(() => { vi.unstubAllGlobals() })

describe('the bundled Home Assistant logo', () => {
  it('is a square PNG big enough for the 256 px space icon', () => {
    const png = readFileSync(join(ASSET_DIR, 'icon.png'))
    expect(png.subarray(1, 4).toString('ascii')).toBe('PNG')
    // IHDR: width / height are big-endian u32 at bytes 16 and 20.
    const w = png.readUInt32BE(16)
    const h = png.readUInt32BE(20)
    expect(w).toBe(h)
    expect(w).toBeGreaterThanOrEqual(256)
  })

  it('ships its source and terms beside it and with the build', () => {
    expect(existsSync(join(ASSET_DIR, 'NOTICE.txt'))).toBe(true)
    expect(existsSync(join(SRC, '..', 'public', 'assets', 'HomeAssistant-logo-NOTICE.txt'))).toBe(true)
  })
})

describe('haLogo', () => {
  it('is bundled with the SPA — no third-party host', () => {
    expect(haLogoUrl).toMatch(/\.png$/)
    expect(haLogoUrl).not.toMatch(/^[a-z]+:\/\//i)
    expect(haLogoUrl).not.toMatch(/^\/\//)
  })

  it('reads the bundled logo into a PNG File named like a picked picture', async () => {
    const fetchMock = vi.fn(async () => new Response('png-bytes'))
    vi.stubGlobal('fetch', fetchMock)
    const file = await haLogoFile()
    expect(fetchMock).toHaveBeenCalledWith(haLogoUrl)
    expect(file.name).toBe('home-assistant.png')
    expect(file.type).toBe('image/png')
    expect(await file.text()).toBe('png-bytes')
  })

  it('a missing asset is an error, not an empty upload', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('nope', { status: 404 })))
    await expect(haLogoFile()).rejects.toThrow('HTTP 404')
  })
})
