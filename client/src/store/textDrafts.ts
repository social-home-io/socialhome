/**
 * Unsent text, one draft per thing being written to.
 *
 * Composers that are reused for different targets — the comment sheet
 * opening post after post, the post composer moving from one space to
 * the next — must not carry text from one target to another, or a
 * comment meant for post A goes out under post B. Each composer asks
 * for the draft of *its* target (``comment:{postId}``,
 * ``comment:{postId}:reply:{commentId}``, ``composer:space:{spaceId}``)
 * and binds its field to that.
 *
 * In memory only, on purpose: a draft lives while the app is open and
 * is gone after a reload. Nothing is written to storage.
 */
import { signal, type Signal } from '@preact/signals'

export interface TextDraft {
  /** What is typed and not sent yet. */
  readonly text: Signal<string>
  /** A send of this draft is in flight (blocks only this draft's send). */
  readonly sending: Signal<boolean>
}

const drafts = new Map<string, TextDraft>()

/** The draft for ``key`` — the same signals on every call, so a
 *  re-render or a remount finds the text again. Entries stay (an empty
 *  one is two tiny signals): a field still bound to a draft never ends up
 *  writing to a forgotten one. */
export function textDraft(key: string): TextDraft {
  let d = drafts.get(key)
  if (!d) {
    d = { text: signal(''), sending: signal(false) }
    drafts.set(key, d)
  }
  return d
}

/** After a successful send: empty the text. Pass the ``sent`` text and
 *  the draft is emptied only if it still holds exactly that — words typed
 *  while the send was on its way are kept. */
export function clearTextDraft(key: string, sent?: string): void {
  const d = drafts.get(key)
  if (!d) return
  if (sent !== undefined && d.text.value !== sent) return
  d.text.value = ''
}
