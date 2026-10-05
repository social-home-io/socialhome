import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

// ── Test harness: stub the browser APIs the component depends on ───────

type Listener = (e: any) => void
class FakeWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static CLOSED = 3
  static instances: FakeWebSocket[] = []
  url: string
  readyState = 0
  binaryType = ''
  onopen: Listener | null = null
  onmessage: Listener | null = null
  onerror: Listener | null = null
  onclose: Listener | null = null
  sent: Array<string | ArrayBuffer> = []
  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
    queueMicrotask(() => {
      this.readyState = 1
      this.onopen?.({})
    })
  }
  send(data: string | ArrayBuffer) { this.sent.push(data) }
  close() { this.readyState = 3; this.onclose?.({}) }
  emitMessage(data: string) { this.onmessage?.({ data }) }
}

class FakeAudioContext {
  static last: FakeAudioContext | null = null
  sampleRate = 48000
  audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) }
  constructor() { FakeAudioContext.last = this }
  createMediaStreamSource = vi.fn().mockReturnValue({
    connect: vi.fn(), disconnect: vi.fn(),
  })
  close = vi.fn().mockResolvedValue(undefined)
}

class FakeAudioWorkletNode {
  port = { onmessage: null as Listener | null, postMessage: vi.fn() }
  connect = vi.fn()
  disconnect = vi.fn()
  constructor(_ctx: any, _name: string, _opts?: any) {}
}

const fakeStream = {
  getTracks: () => [{ stop: vi.fn() }],
} as any

beforeEach(() => {
  vi.resetModules()
  FakeWebSocket.instances.length = 0
  ;(globalThis as any).WebSocket = FakeWebSocket
  ;(globalThis as any).AudioContext = FakeAudioContext
  ;(globalThis as any).AudioWorkletNode = FakeAudioWorkletNode
  FakeAudioContext.last = null
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn().mockResolvedValue(fakeStream) },
  })
})

afterEach(() => { vi.restoreAllMocks() })

describe('SttButton', () => {
  it('module exports exist', async () => {
    const mod = await import('./SttButton')
    expect(mod.SttButton).toBeTruthy()
  })

  it('renders an idle mic button by default', async () => {
    const { SttButton } = await import('./SttButton')
    const { getByRole } = render(<SttButton onText={() => {}} />)
    const btn = getByRole('button')
    // Idle state renders an inline SVG mic icon (replaced the
    // ``🎙`` emoji because that rendered as tofu on Linux desktops
    // without an emoji font). The SVG carries no text, so we assert
    // its presence + the recognisable aria-label instead.
    expect(btn.querySelector('svg')).not.toBeNull()
    expect(btn.getAttribute('aria-label')).toMatch(/voice note/i)
    expect(btn.getAttribute('aria-pressed')).toBe('false')
  })

  it('opens a WS and sends a start frame on press', async () => {
    const { SttButton } = await import('./SttButton')
    const { getByRole } = render(<SttButton onText={() => {}} language="de" />)
    const btn = getByRole('button')

    fireEvent.mouseDown(btn)
    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1))
    const ws = FakeWebSocket.instances[0]
    // Relative URL — modern browsers resolve it against the document
    // base (and auto-rewrite ``http``/``https`` → ``ws``/``wss``).
    expect(ws.url).toContain('api/stt/stream')

    await waitFor(() => {
      const starts = ws.sent.filter(s => typeof s === 'string') as string[]
      expect(starts.length).toBeGreaterThan(0)
    })
    const start = JSON.parse(ws.sent[0] as string)
    expect(start).toMatchObject({
      type: 'start', language: 'de', sample_rate: 16000, channels: 1,
    })
  })

  it('loads the worklet from a same-origin asset, never a blob: URL (CSP script-src)', async () => {
    const { SttButton } = await import('./SttButton')
    const { getByRole } = render(<SttButton onText={() => {}} />)
    fireEvent.mouseDown(getByRole('button'))
    await waitFor(() => expect(FakeAudioContext.last).not.toBeNull())
    const addModule = FakeAudioContext.last!.audioWorklet.addModule
    await waitFor(() => expect(addModule).toHaveBeenCalledTimes(1))
    const url = String(addModule.mock.calls[0][0])
    expect(url).toMatch(/sttPcm16Worklet\.js$/)
    expect(url.startsWith('blob:')).toBe(false)
  })

  it('forwards a final transcript to onText and returns to idle', async () => {
    const onText = vi.fn()
    const { SttButton } = await import('./SttButton')
    const { getByRole } = render(<SttButton onText={onText} />)
    const btn = getByRole('button')

    fireEvent.mouseDown(btn)
    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1))
    const ws = FakeWebSocket.instances[0]
    await waitFor(() => expect(ws.sent.length).toBeGreaterThan(0))
    fireEvent.mouseUp(btn)

    ws.emitMessage(JSON.stringify({ type: 'final', text: 'hello world' }))
    await waitFor(() => expect(onText).toHaveBeenCalledWith('hello world'))
  })

  it('hides itself after an "unsupported" error frame', async () => {
    const { SttButton } = await import('./SttButton')
    const { getByRole, queryByRole } = render(<SttButton onText={() => {}} />)
    const btn = getByRole('button')

    fireEvent.mouseDown(btn)
    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1))
    const ws = FakeWebSocket.instances[0]
    await waitFor(() => expect(ws.sent.length).toBeGreaterThan(0))

    ws.emitMessage(JSON.stringify({
      type: 'error', detail: 'STT is not configured on this server.',
    }))

    await waitFor(() => expect(queryByRole('button')).toBeNull())
  })
})
