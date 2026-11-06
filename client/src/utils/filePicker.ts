/**
 * filePicker — whether a file input may let the person pick many files.
 *
 * The Home Assistant Android app before 2026.11.1 reads the system
 * picker's result with ``FileChooserParams.parseResult``, which ignores
 * the ``ClipData`` a multi-pick comes back in. With ``multiple`` on the
 * input the picker answers in ``ClipData`` — on Samsung even for a single
 * photo — so the page gets no file and no ``change``: "pick photos →
 * nothing happens" (home-assistant/android#7548, fixed by #7549 in
 * 2026.11.1). Social Home runs inside that app's WebView in ``haos`` /
 * ``ha`` mode, so there a picker asks for one file at a time.
 *
 * Every ``<input type="file">`` that would take ``multiple`` uses
 * ``multiple={fileInputMultiple()}`` — never a bare ``multiple`` (a
 * guard in ``filePicker.test.ts`` rejects one). Drag-and-drop is not
 * affected.
 *
 * The app appends ``Home Assistant/<version> (Android …)`` to the
 * WebView's user agent (``HAWebView.kt`` / ``HomeAssistantApis.kt`` in
 * home-assistant/android); the iOS app's WKWebView never had the bug.
 *
 * Temporary: once HA Android apps before 2026.11.1 are rare (early
 * 2027), drop the version check and let ``fileInputMultiple()`` return
 * ``true`` — keep the helper, its guard test and the harness check.
 */

/** The first HA Android app release that delivers multi-picks. */
const FIXED: readonly [number, number, number] = [2026, 11, 1]

const HA_ANDROID = /Home Assistant\/(\S+) \(Android/

/** May a file picker on this device return several files? */
export function fileInputMultiple(
  ua: string = typeof navigator === 'undefined' ? '' : navigator.userAgent,
): boolean {
  const m = HA_ANDROID.exec(ua)
  if (!m) return true
  const v = /^(\d+)\.(\d+)\.(\d+)/.exec(m[1])
  // A version we can't read (a dev build): one file at a time is safe.
  if (!v) return false
  const have = [Number(v[1]), Number(v[2]), Number(v[3])]
  for (let i = 0; i < 3; i++) {
    if (have[i] !== FIXED[i]) return have[i] > FIXED[i]
  }
  return true
}
