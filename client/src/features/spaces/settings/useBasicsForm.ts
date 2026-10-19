/**
 * The Basics form: the space's name, short description, emoji and About
 * text — the one part of space settings that is edited as a draft and
 * saved with the page's save bar (owner decision 8). Pictures, quick
 * links and the theme save on their own.
 *
 * The draft lives in the settings page (not the section), so the save bar
 * can sit at page level and a section jump never drops it. A reload (or
 * another admin's rename) moves every field the viewer hasn't touched to
 * the new stored value and leaves edited ones alone.
 */
import { useLayoutEffect, useRef } from 'preact/hooks'
import { useSignal, type Signal } from '@preact/signals'

export interface BasicsValues {
  name: string
  description: string
  emoji: string
  about: string
}

/** What the stored space says, with absent values as ''. */
export function basicsOf(space: {
  name: string
  description?: string | null
  emoji?: string | null
  about_markdown?: string | null
}): BasicsValues {
  return {
    name: space.name,
    description: space.description || '',
    emoji: space.emoji || '',
    about: space.about_markdown || '',
  }
}

const BODY_KEY: Record<keyof BasicsValues, string> = {
  name: 'name',
  description: 'description',
  emoji: 'emoji',
  about: 'about_markdown',
}

/** The PATCH body for the fields the draft changed ('' clears a field). */
export function basicsChanges(
  draft: BasicsValues, stored: BasicsValues,
): Record<string, string> {
  const body: Record<string, string> = {}
  for (const k of Object.keys(BODY_KEY) as (keyof BasicsValues)[]) {
    if (draft[k] !== stored[k]) body[BODY_KEY[k]] = draft[k]
  }
  return body
}

export interface BasicsForm {
  fields: { [K in keyof BasicsValues]: Signal<string> }
  /** The PATCH body of what changed; empty when nothing did. */
  changes: Record<string, string>
  dirtyCount: number
  reset: () => void
}

export function useBasicsForm(stored: BasicsValues): BasicsForm {
  const fields = {
    name: useSignal(stored.name),
    description: useSignal(stored.description),
    emoji: useSignal(stored.emoji),
    about: useSignal(stored.about),
  }
  const previous = useRef(stored)
  const key = JSON.stringify(stored)
  // Layout effect: the draft follows the stored values before paint, so a
  // fresh load never flashes the save bar.
  useLayoutEffect(() => {
    for (const k of Object.keys(fields) as (keyof BasicsValues)[]) {
      if (fields[k].value === previous.current[k]) fields[k].value = stored[k]
    }
    previous.current = stored
  }, [key])
  const draft: BasicsValues = {
    name: fields.name.value,
    description: fields.description.value,
    emoji: fields.emoji.value,
    about: fields.about.value,
  }
  const changes = basicsChanges(draft, stored)
  return {
    fields,
    changes,
    dirtyCount: Object.keys(changes).length,
    reset: () => {
      for (const k of Object.keys(fields) as (keyof BasicsValues)[]) {
        fields[k].value = stored[k]
      }
    },
  }
}
