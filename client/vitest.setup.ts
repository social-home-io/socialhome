// Global test setup: unmount the testing-library tree after every test so
// rendered DOM never bleeds between tests. Vitest 4 + @testing-library/preact
// no longer auto-register this, so tests that don't manually call cleanup()
// would otherwise accumulate DOM and hit "multiple elements found".
import { options } from 'preact'
import { act, cleanup } from '@testing-library/preact'
import { afterEach } from 'vitest'

// preact/hooks schedules effects with ``afterNextFrame``: a
// requestAnimationFrame plus a 35 ms setTimeout fallback that calls
// ``cancelAnimationFrame``. When that fallback fires after vitest has
// torn jsdom down (likely under load), the global is gone and the run
// reports "ReferenceError: cancelAnimationFrame is not defined" as an
// unhandled error. A plain setTimeout scheduler never touches rAF.
options.requestAnimationFrame = (cb: () => void) => { setTimeout(cb, 0) }

// Preact 11 runs useEffect cleanups of unmounted components after paint,
// not synchronously. testing-library's cleanup() doesn't wrap the unmount
// in act(), so without it a test's cleanups (hang up a call, clear a
// signal…) fire later — inside the *next* test. act() flushes them here.
// It is a safety net, not a crutch: every test must also pass with plain
// cleanup() (unique ids per test, module state reset in beforeEach). The
// suite was verified that way; InCallPage.test.tsx keeps a regression.
afterEach(() => {
  act(() => cleanup())
})
