/* global AudioWorkletProcessor, sampleRate, registerProcessor -- AudioWorkletGlobalScope */
/* AudioWorklet processor for SttButton (push-to-talk STT).
 *
 * Downsamples Float32 mono frames to 16 kHz PCM16 LE and posts each
 * ~20 ms buffer back to the main thread, which forwards it to the STT
 * WebSocket. Loaded via ``new URL('./sttPcm16Worklet.js',
 * import.meta.url)`` so Vite emits it as a same-origin asset: a Blob
 * URL would need ``blob:`` in the CSP's ``script-src``
 * (socialhome/csp.py), which this keeps out.
 */
class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor(opts) {
    super();
    const po = (opts && opts.processorOptions) || {};
    this.sourceRate = po.sourceRate || sampleRate;
    this.targetRate = po.targetRate || 16000;
    this.ratio = this.sourceRate / this.targetRate;
    this.buffer = [];
    this.acc = 0;
    this.chunkSize = Math.round(this.targetRate * 0.02); // 20ms frames
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch = input[0];
    for (let i = 0; i < ch.length; i++) {
      this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        const s = Math.max(-1, Math.min(1, ch[i]));
        this.buffer.push(s < 0 ? s * 0x8000 : s * 0x7fff);
        if (this.buffer.length >= this.chunkSize) {
          const out = new Int16Array(this.buffer);
          this.buffer = [];
          this.port.postMessage(out.buffer, [out.buffer]);
        }
      }
    }
    return true;
  }
}
registerProcessor('stt-pcm16-downsampler', Pcm16Downsampler);
