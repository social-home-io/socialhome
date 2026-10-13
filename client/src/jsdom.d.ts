/**
 * The slice of jsdom's API the tests use (jsdom ships without types and
 * `@types/jsdom` isn't a dependency): a fresh window to run a classic
 * script in, isolated from the test's own document.
 */
declare module 'jsdom' {
  export class JSDOM {
    constructor(html?: string, options?: { runScripts?: 'outside-only' | 'dangerously'; url?: string })
    readonly window: Window & typeof globalThis
  }
}
