"""Refresh the bundled Twemoji subset (``socialhome/dashboard/static/emoji``).

A developer tool — never run by Social Home itself (nothing is fetched at
runtime). It collects every emoji the household dashboard can show:

* every emoji string literal in ``socialhome/dashboard/*.py`` (the title
  icon tables, picture view, map and message glyphs), the Home Assistant
  zone icon map (``socialhome/platform/ha/zones.py``) and the dashboard
  templates;
* the SPA's timetable icon presets and reaction emoji (what a lesson
  icon or a status is most likely to be);
* :data:`EXTRA` — a common set for event titles and statuses;

and downloads each 72 × 72 PNG from the pinned Twemoji release
(github.com/jdecked/twemoji, graphics CC-BY 4.0). Emoji Twemoji lacks are
skipped (the API falls back to 📍).

Usage: ``python scripts/fetch_twemoji.py`` from the repository root.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from socialhome.dashboard.emoji import EMOJI_DIR, FALLBACK_CODE, emoji_code  # noqa: E402
from socialhome.domain.household_dashboard import leading_emoji  # noqa: E402
from socialhome.platform.ha.zones import MDI_ZONE_EMOJI  # noqa: E402

TAG = "v17.0.3"
BASE = f"https://raw.githubusercontent.com/jdecked/twemoji/{TAG}/assets/72x72/"

SOURCES = (
    *sorted((ROOT / "socialhome/dashboard").glob("*.py")),
    *sorted((ROOT / "socialhome/dashboard/templates").glob("*.j2")),
    ROOT / "socialhome/platform/ha/zones.py",
    ROOT / "client/src/features/timetable/icons.ts",
    ROOT / "client/src/data/emojis.ts",
)

EXTRA = (
    # Faces and status
    "😀😃😄😁😆😅🤣😂🙂🙃😉😊😇🥰😍🤩😘😗😚😙😋😛😜🤪😝🤑🤗🤭🤫🤔🤐🤨😐😑😶"
    "😏😒🙄😬😌😔😪🤤😴😷🤒🤕🤢🤮🤧🥵🥶🥴😵🤯🤠🥳😎🤓🧐😕😟🙁😮😯😲😳🥺😦😧"
    "😨😰😥😢😭😱😖😣😞😓😩😫🥱😤😡😠🤬😈👿💀💩🤡👻👽🤖😺❤🧡💛💚💙💜🤎🖤🤍💔"
    "💕💖💗💘💝💯💢💥💫💦💨💬💭💤👋🤚✋🖖👌🤌✌🤞🤟🤘🤙👈👉👆👇☝👍👎✊👊👏🙌👐"
    "🤲🤝🙏💪🧠👀👶🧒👦👧🧑👨👩🧓👴👵👪"
    # Family life, home, school
    "🏠🏡🏢🏫🏥🏦🏪🏬🏭🏰⛪🕌⛺🛏🛋🚪🪑🚽🚿🛁🧹🧺🧻🧼🧽🪣🧴🔑🗝🔒🔓🔔📣📢🎒✏✒🖊🖍📝"
    "📒📓📔📕📗📘📙📚📖🔖📎📌📍✂📐📏🧮🔬🔭🧪🧫🧬💻🖥⌨🖱📱☎📞📺📷📸📹🎥📽🎞🎬"
    "🎤🎧🎵🎶🎹🥁🎷🎺🎸🪕🎻🎨🖌🧶🧵🪡🎭🎪🎟🎫🎮🕹🎲🧩♟🧸🪀🪁🎈🎉🎊🎁🎀🎂🍰🧁🕯"
    # Sports
    "⚽⚾🥎🏀🏐🏈🏉🎾🥏🎳🏏🏑🏒🥍🏓🏸🥊🥋🥅⛳⛸🎣🤿🎽🎿🛷🥌🎯🏹🛹🛼🏋🤸🤼🤽🤾🤺🧗🚴🚵🏇"
    "🏊🏄🚣🧘🏃🚶💃🕺🏆🏅🥇🥈🥉"
    # Food
    "🍎🍏🍐🍊🍋🍌🍉🍇🍓🫐🍈🍒🍑🥭🍍🥥🥝🍅🍆🥑🥦🥬🥒🌶🌽🥕🧄🧅🥔🍠🥐🥯🍞🥖🥨🧀🥚"
    "🍳🧈🥞🧇🥓🥩🍗🍖🌭🍔🍟🍕🥪🥙🧆🌮🌯🥗🥘🥫🍝🍜🍲🍛🍣🍱🥟🍤🍙🍚🍘🍥🥠🍢🍡🍧🍨🍦"
    "🥧🍫🍬🍭🍮🍯🍼🥛☕🍵🧃🥤🧋🍶🍺🍻🥂🍷🥃🍸🍹🧉🧊🥢🍽🍴🥄🔪🛒"
    # Nature, weather, animals
    "☀🌤⛅🌥☁🌦🌧⛈🌩🌨❄☃⛄🌬🌪🌫🌈☂☔⚡🔥💧🌊🌙🌛🌜🌝🌞⭐🌟✨☄🌍🌎🌏🌋⛰🏔🗻🏕🏖"
    "🏜🏝🏞🌲🌳🌴🌵🌷🌸🌹🌺🌻🌼💐🌾🌿☘🍀🍁🍂🍃🍄🌰🐶🐕🐩🐺🦊🐱🐈🦁🐯🐴🐎🦄🐮🐷"
    "🐗🐑🐐🐪🐘🦒🐭🐹🐰🐇🦔🐻🐨🐼🦘🐔🐓🐣🐤🐥🐦🐧🕊🦅🦆🦉🐸🐢🦎🐍🐲🐉🐳🐬🐟🐠🐡"
    "🦈🐙🐚🐌🦋🐛🐜🐝🐞🦗🕷🦂🦀🦞🦐🦑"
    # Travel and places
    "🚗🚕🚙🚌🚎🏎🚓🚑🚒🚐🚚🚛🚜🛴🚲🛵🏍🚨🚔🚍🚘🚖🚡🚠🚟🚃🚋🚞🚝🚄🚅🚈🚂🚆🚇🚊🚉✈🛫"
    "🛬🛩💺🚁🚀🛸⛵🚤🛥🛳⛴🚢⚓⛽🚧🚦🚥🗺🗽🗼🏟🎡🎢🎠⛲🏗🌁🌃🏙🌄🌅🌆🌇🌉🎑🏞"
    # Objects, time, symbols
    "⌚⏰⏱⏲🕰⌛⏳📅📆🗓📋📊📈📉💡🔦🏮💰💳💶💵✉📧📨📦📫📬📭📮🗑🔧🔨⚒🛠⛏🪛🔩⚙"
    "🧰🧲🪜💊💉🩹🩺🦷🦴👓🕶👔👕👖🧣🧤🧥🧦👗👘👙👚👛👜👝🎓🧢👒🎩👟👞👠👡👢🥾"
    "💍💎⚠⛔🚫❌⭕✅☑✔➕➖❗❓🔴🟠🟡🟢🔵🟣⚪⚫🔶🔷🔸🔹🔺🔻💠🔘🔁🔂▶⏸⏹⏺⏭⏮"
    "🔊🔇🔉🔈📶♻🏁🚩🎌🏳"
    # Flags of the dashboard's locales' countries
    "🇬🇧🇺🇸🇩🇪🇦🇹🇨🇭🇪🇸🇫🇷🇳🇱🇧🇪🇮🇹🇵🇹🇪🇺"
)


def _emoji_in(text: str) -> set[str]:
    """Every emoji at the start of any run of non-ASCII text in ``text``."""
    found: set[str] = set()
    for i in range(len(text)):
        if ord(text[i]) < 0x80:
            continue
        hit = leading_emoji(text[i:])
        if hit:
            found.add(hit)
    return found


def wanted() -> set[str]:
    codes: set[str] = {FALLBACK_CODE}
    # Zone icons may be enclosed letters (🅿️), which the scan skips.
    codes |= {c for c in map(emoji_code, MDI_ZONE_EMOJI.values()) if c}
    for path in SOURCES:
        for emoji in _emoji_in(path.read_text(encoding="utf-8")):
            code = emoji_code(emoji)
            if code:
                codes.add(code)
    for emoji in _emoji_in(EXTRA):
        code = emoji_code(emoji)
        if code:
            codes.add(code)
    return codes


def main() -> int:
    EMOJI_DIR.mkdir(parents=True, exist_ok=True)
    codes = sorted(wanted())
    have = {p.stem for p in EMOJI_DIR.glob("*.png")}
    missing = []
    for code in codes:
        if code in have:
            continue
        try:
            with urllib.request.urlopen(BASE + f"{code}.png", timeout=30) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                missing.append(code)
                continue
            raise
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise SystemExit(f"{code}: not a PNG")
        (EMOJI_DIR / f"{code}.png").write_bytes(body)
    for stale in sorted(have - set(codes)):
        (EMOJI_DIR / f"{stale}.png").unlink()
    print(f"{len(codes) - len(missing)} emoji bundled; not in Twemoji: {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
