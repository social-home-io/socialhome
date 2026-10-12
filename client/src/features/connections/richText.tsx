/**
 * fillNodes — render a translated sentence whose ``{placeholders}`` are
 * JSX nodes (a bold name, an italic phrase, an inline link-button), so
 * word order stays the translator's choice.
 */
import type { ComponentChildren } from 'preact'
import { Fragment } from 'preact'

export function fillNodes(
  text: string,
  nodes: Record<string, ComponentChildren>,
): ComponentChildren {
  const parts = text.split(/\{([A-Za-z0-9_]+)\}/)
  return parts.map((part, i) =>
    i % 2 === 1
      ? <Fragment key={i}>{part in nodes ? nodes[part] : `{${part}}`}</Fragment>
      : part,
  )
}

/** Locale-aware "A, B and C" list. Falls back to a comma join. */
export function joinNames(names: string[], locale: string): string {
  try {
    return new Intl.ListFormat(locale, { style: 'long', type: 'conjunction' }).format(names)
  } catch {
    return names.join(', ')
  }
}
