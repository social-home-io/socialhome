# Emoji images — Twemoji

The PNG files in this directory are **Twemoji** graphics, 72 × 72, from
<https://github.com/jdecked/twemoji> release **v17.0.3**
(`assets/72x72/`).

Copyright 2019 Twitter, Inc and other contributors; Copyright 2024 jdecked
and other contributors.

The graphics are licensed under **CC-BY 4.0**:
<https://creativecommons.org/licenses/by/4.0/>. They are bundled unchanged
(Social Home only scales them on request for wall panels,
`GET /api/display/v1/emoji/{code}`).

Only the subset the household dashboard can show is bundled; refresh it
with `python scripts/fetch_twemoji.py`. File names are Twemoji code
points (`1f3ca.png` is 🏊).
