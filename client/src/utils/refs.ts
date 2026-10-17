import type { Ref } from 'preact'

/** Point ``ref`` — an object or a callback ref, as a component receives it
 *  in Preact 11 — at ``value``. For a component that keeps its own ref on
 *  an element and also forwards the caller's ``ref`` to it. */
export function assignRef<T>(ref: Ref<T> | undefined, value: T | null): void {
  if (typeof ref === 'function') ref(value)
  else if (ref) ref.current = value
}
