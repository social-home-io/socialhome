/**
 * Missing-feature names in the UI language.
 *
 * The compat endpoints (``GET /api/admin/federation/compat``,
 * ``GET /api/spaces/{id}/compat``) send each feature twice, in the same
 * order: an English label (``lacking_features`` / ``lagging_features``)
 * and a stable slug (``lacking_feature_keys`` / ``lagging_feature_keys``).
 * The slug is translated as ``capability.<slug>`` — every key is
 * ``capability.`` + one slug of ``CAPABILITY_FEATURE_KEYS`` in
 * ``socialhome/domain/federation_capabilities.py``.
 */
import { t } from '@/i18n/i18n'

/** One feature name: the translation of ``capability.<key>``, or the
 *  server's English ``label`` when this build has no such key (a newer
 *  server) or the server sent no key. */
export function featureLabel(label: string, key?: string): string {
  if (!key) return label
  const id = `capability.${key}`
  const text = t(id)
  return text === id ? label : text
}

/** :func:`featureLabel` over parallel label / key lists. */
export function featureLabels(labels: string[], keys?: string[]): string[] {
  return labels.map((label, i) => featureLabel(label, keys?.[i]))
}
