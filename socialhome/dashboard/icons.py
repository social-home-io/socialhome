"""Emoji for titles — what the picture view and Home's narrow bars show.

A child who can't read yet still recognises 🏊 or 🦷, so the dashboard
turns event, chore and lesson titles into one emoji:

1. a title that already starts with an emoji ("🏊 Swimming") uses it;
2. otherwise a keyword table guesses one, in en / de / es / fr / nl —
   the same approach as the SPA's ``suggestIcon``
   (``client/src/features/timetable/icons.ts``), whose subject table
   :data:`LESSON_KEYWORDS` mirrors;
3. otherwise the neutral :data:`FALLBACK_ICON` (🔹 — never ⭐, which
   reads as "reward").

Matching runs on *normalised words*: lowercase (``casefold``, so "ß" is
"ss"), diacritics stripped ("Zähne" → "zahne"), split on anything that
isn't a letter. A keyword then matches by its form:

* ``"=bed"`` — a whole word only ("Bed", not "Bedford");
* ``"^shop"`` — the start of a word ("Shopping", not "Workshop");
* ``"*mull"`` — anywhere inside a word ("Restmüll");
* unmarked — by length: up to 3 letters a whole word, 4 letters the
  start of a word, 5 or more anywhere inside a word, so German
  compounds work ("Kinderschwimmen", "Fensterputzen") while short
  stems can't fire inside unrelated words.

A keyword with a space is a phrase over whole words ("tisch decken").
The first matching row wins, so the specific rows come first
("Zähneputzen" is 🦷, not 🧹).

Pure: no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Final

from ..domain.household_dashboard import leading_emoji

__all__ = [
    "BREAK_ICON",
    "FALLBACK_ICON",
    "GENERAL_KEYWORDS",
    "LESSON_KEYWORDS",
    "normalized_words",
    "suggest_icon",
    "title_icon",
]

#: Shown when nothing in a title says what it is.
FALLBACK_ICON: Final = "🔹"
#: A break slot without an icon or a recognisable label.
BREAK_ICON: Final = "🍎"

_Table = Sequence[tuple[str, Sequence[str]]]

#: School subjects and break slots — the SPA's ``KEYWORDS`` table
#: (``client/src/features/timetable/icons.ts``), with its emoji and its
#: rules: whole word for ≤ 3 letters and ``$``-marked keywords
#: (here ``=``), else the start of a word (``^``).
LESSON_KEYWORDS: Final[_Table] = (
    ("🔢", ("^math", "^rechnen", "^matematica", "^wiskunde")),
    ("🧮", ("^numeracy", "^arithmetic", "^rekenen", "^calcul")),
    (
        "🔬",
        (
            "^science",
            "^nawi",
            "^naturwissenschaft",
            "^biolog",
            "=bio",
            "^chemi",
            "^physi",
            "^ciencias",
            "^natuurkunde",
            "^scheikunde",
        ),
    ),
    ("🏊", ("^schwimm", "^swim", "^natacion", "^natation", "^zwem")),
    (
        "⚽",
        (
            "^sport",
            "^turnen",
            "=pe",
            "=eps",
            "^educacion fisica",
            "=gym",
            "=lo",
            "^gymnastik",
            "^bewegung",
        ),
    ),
    ("🎵", ("^music", "^musik", "^musica", "^musique", "^muziek")),
    ("🇬🇧", ("^englisch", "^english", "^ingles", "^anglais", "^engels")),
    ("🇫🇷", ("^franzosisch", "^french", "^francais", "^frances", "^frans")),
    ("🇪🇸", ("^spanisch", "^spanish", "^espanol", "^espagnol", "^spaans")),
    ("🇩🇪", ("^german", "^allemand", "^aleman", "^duits")),
    (
        "📖",
        ("^deutsch", "^reading", "^lesen", "^lecture", "^lezen", "^lengua", "^lectura"),
    ),
    ("✏️", ("^writing", "^schreib", "^ecriture", "^escritura", "^schrijven")),
    (
        "🎨",
        (
            "=art",
            "^arte",
            "^arts",
            "^kunst",
            "=bk",
            "^bildnerisch",
            "^zeichnen",
            "^dibujo",
            "^dessin",
            "^tekenen",
        ),
    ),
    ("🌍", ("^geograph", "^geografi", "^erdkunde", "^aardrijkskunde")),
    ("🕰️", ("^geschichte", "^history", "^histoire", "^historia", "^geschiedenis")),
    ("💻", ("^informati", "^computing", "^computer", "=ict")),
    (
        "🌱",
        (
            "^nature",
            "^natur",
            "^sachkunde",
            "^sachunterricht",
            "=hsu",
            "^natuur",
            "^naturaleza",
        ),
    ),
    ("🎭", ("^drama", "^theater", "^theatre", "^teatro", "^toneel")),
    (
        "🧑‍🍳",
        ("^kochen", "^cooking", "^cuisine", "^cocina", "^koken", "^hauswirtschaft"),
    ),
    (
        "🙏",
        ("^religion", "^reli", "^ethik", "^ethics", "^ethique", "^etica", "^religie"),
    ),
    (
        "🍎",
        (
            "^snack",
            "^breakfast",
            "^jause",
            "^znuni",
            "^fruhstuck",
            "^gouter",
            "^merienda",
            "^pausenbrot",
        ),
    ),
    (
        "🍽️",
        (
            "^mittag",
            "^lunch",
            "^cantine",
            "^comedor",
            "^mensa",
            "^dejeuner",
            "^almuerzo",
        ),
    ),
    (
        "🧸",
        (
            "^pause",
            "^break",
            "^recreo",
            "^recre",
            "^recreation",
            "^speelkwartier",
            "^pauze",
        ),
    ),
    ("🚌", ("=bus", "^schulbus")),
    (
        "🏠",
        (
            "=home",
            "=heim",
            "^heimweg",
            "=hause",
            "^zuhause",
            "=maison",
            "=casa",
            "=huis",
        ),
    ),
)

#: Everyday things on a family calendar or chore list — most specific
#: first.
GENERAL_KEYWORDS: Final[_Table] = (
    # ── Body care + appointments ──
    (
        "🦷",
        (
            "zahn",
            "zaehn",
            "teeth",
            "tooth",
            "dentist",
            "^diente",
            "=dents",
            "^tanden",
            "^tandarts",
        ),
    ),
    (
        "🩺",
        (
            "doctor",
            "*arzt",
            "docteur",
            "medecin",
            "medico",
            "dokter",
            "huisarts",
            "klinik",
            "clinic",
            "kliniek",
            "pediatr",
        ),
    ),
    ("💉", ("impfung", "^impfen", "vaccin", "vacuna", "=jab")),
    ("💊", ("medicine", "medikament", "medicina", "medicament", "medicijn", "vitamin")),
    (
        "💇",
        ("friseur", "frisor", "haircut", "hairdress", "peluquer", "coiffeur", "kapper"),
    ),
    # ── Celebrations ──
    (
        "🎂",
        ("birthday", "geburtstag", "cumplean", "anniversaire", "verjaardag", "=jarig"),
    ),
    (
        "🎄",
        (
            "weihnacht",
            "christmas",
            "xmas",
            "navidad",
            "=kerst",
            "^kerstmis",
            "^kerstfeest",
            "^kerstavond",
        ),
    ),
    ("🐣", ("ostern", "easter", "pascua", "=paques", "=pasen")),
    ("🎃", ("halloween",)),
    (
        "🎉",
        (
            "baby shower",
            "party",
            "fiesta",
            "=feier",
            "=feiern",
            "^feiertag",
            "^abschlussfeier",
            "=fete",
            "feest",
        ),
    ),
    # ── Meals ──
    (
        "🥣",
        (
            "breakfast",
            "fruhstuck",
            "fruehstueck",
            "desayun",
            "petit dejeuner",
            "ontbijt",
        ),
    ),
    ("🍕", ("pizza",)),
    (
        "🍝",
        (
            "lunch",
            "^mittagessen",
            "abendessen",
            "abendbrot",
            "dinner",
            "supper",
            "almuerzo",
            "=comida",
            "=cena",
            "dejeuner",
            "=diner",
            "souper",
            "avondeten",
            "middageten",
            "=essen",
        ),
    ),
    ("🍎", ("snack", "jause", "znuni", "zvieri", "merienda", "gouter", "tussendoor")),
    (
        "🍪",
        (
            "=backen",
            "^kuchenback",
            "^platzchenback",
            "^plaetzchenback",
            "^brotback",
            "baking",
            "=bake",
            "hornear",
            "patisser",
            "=bakken",
            "cookie",
            "^kuchen",
            "^platzchen",
            "^plaetzchen",
            "galleta",
            "gateau",
        ),
    ),
    ("🍳", ("cooking", "kochen", "cocinar", "cuisiner", "=koken", "^kook")),
    # ── Chores ──
    (
        "🍽️",
        (
            "dishes",
            "dishwasher",
            "geschirr",
            "spulmasch",
            "spulen",
            "abwasch",
            "=platos",
            "vaisselle",
            "=afwas",
            "afwassen",
            "vaatwas",
            "lavavajilla",
            "set the table",
            "clear the table",
            "tisch decken",
            "tischdecken",
            "tisch abdecken",
            "tisch abraumen",
            "mettre la table",
            "poner la mesa",
            "tafel dekken",
        ),
    ),
    ("♻️", ("recycl", "altpapier", "altglas", "gelber sack", "reciclaj")),
    (
        "🗑️",
        (
            "trash",
            "garbage",
            "rubbish",
            "=bin",
            "=bins",
            "=mull",
            "^mulltonne",
            "^mulleimer",
            "^mullabfuhr",
            "^restmull",
            "^biomull",
            "=muell",
            "^muelltonne",
            "^muelleimer",
            "^restmuell",
            "^biomuell",
            "abfall",
            "basura",
            "poubelle",
            "ordures",
            "vuilnis",
            "=afval",
            "^afvalbak",
            "^afvalzak",
            "^afvalcontainer",
        ),
    ),
    (
        "🧺",
        (
            "laundry",
            "=wasche",
            "^waschek",
            "^waschmasch",
            "^wascheauf",
            "colada",
            "lavadora",
            "=linge",
            "lessive",
            "^wasgoed",
            "wasmachine",
            "ironing",
            "bugeln",
            "=planchar",
            "repasser",
            "strijken",
        ),
    ),
    (
        "🧹",
        (
            "clean",
            "tidy",
            "sweep",
            "vacuum",
            "hoover",
            "aufraum",
            "aufraeum",
            "putzen",
            "staubsaug",
            "=fegen",
            "=kehren",
            "^wischen",
            "limpiar",
            "limpieza",
            "ordenar",
            "=barrer",
            "aspirar",
            "nettoy",
            "=ranger",
            "menage",
            "balayer",
            "aspirateur",
            "opruim",
            "schoonma",
            "stofzuig",
            "=poetsen",
            "=vegen",
            "dweilen",
        ),
    ),
    # ── Bedtime + bath ──
    (
        "🛏️",
        (
            "bedtime",
            "=bed",
            "=bett",
            "^bettzeit",
            "^bettgeh",
            "^betthupferl",
            "schlaf",
            "sleep",
            "=nap",
            "siesta",
            "=cama",
            "dormir",
            "=lit",
            "coucher",
            "slapen",
            "^slaap",
            "gutenacht",
            "gute nacht",
            "bonne nuit",
            "buenas noches",
            "goodnight",
            "good night",
            "welterusten",
        ),
    ),
    (
        "🛁",
        (
            "=bath",
            "bathtime",
            "=baden",
            "^badewanne",
            "=bad",
            "^shower",
            "dusch",
            "=bano",
            "^banar",
            "ducha",
            "=bain",
            "douche",
        ),
    ),
    ("👕", ("get dressed", "anzieh", "^umzieh", "^vestir", "habiller", "aankleden")),
    # ── Pets + garden ──
    (
        "🐱",
        (
            "=cat",
            "=cats",
            "kitten",
            "^katze",
            "=gato",
            "=gatos",
            "=chaton",
            "=kat",
            "=poes",
        ),
    ),
    (
        "🐶",
        (
            "=dog",
            "=dogs",
            "puppy",
            "=hund",
            "=hunde",
            "^hundefutter",
            "gassi",
            "welpe",
            "=perro",
            "=chien",
            "=hond",
        ),
    ),
    (
        "🐟",
        (
            "=fish",
            "=fisch",
            "=fische",
            "^fischfutter",
            "=pez",
            "=peces",
            "poisson",
            "=vis",
            "aquarium",
        ),
    ),
    (
        "🐰",
        (
            "rabbit",
            "bunny",
            "kaninchen",
            "=hase",
            "=hasen",
            "conejo",
            "lapin",
            "konijn",
            "hamster",
            "meerschwein",
            "guinea pig",
            "cobaya",
        ),
    ),
    (
        "🐴",
        (
            "^reiten",
            "^reitstunde",
            "^reitunterricht",
            "^reitstall",
            "^reithalle",
            "riding",
            "horse",
            "^pferd",
            "caballo",
            "equitation",
            "=cheval",
            "^paard",
            "=pony",
        ),
    ),
    (
        "🌱",
        (
            "^plant",
            "pflanz",
            "^blume",
            "flower",
            "=flor",
            "=fleurs",
            "bloem",
            "^garden",
            "^garten",
            "^jardin",
            "=tuin",
            "tuinieren",
            "=regar",
            "arroser",
            "=gieten",
        ),
    ),
    # ── Errands + learning ──
    (
        "🛒",
        (
            "^shop",
            "einkauf",
            "grocer",
            "=compra",
            "=compras",
            "=courses",
            "boodschap",
            "supermark",
            "supermerc",
            "supermarch",
        ),
    ),
    (
        "📚",
        (
            "homework",
            "hausaufgabe",
            "=hausi",
            "deberes",
            "=tarea",
            "=tareas",
            "devoirs",
            "huiswerk",
            "nachhilfe",
            "tutoring",
            "=lernen",
            "=study",
            "studying",
            "estudiar",
            "=reviser",
            "=leren",
        ),
    ),
    (
        "📖",
        (
            "=read",
            "reading",
            "lesen",
            "=leer",
            "lectura",
            "=lire",
            "lecture",
            "lezen",
            "=book",
            "=buch",
            "=libro",
            "=livre",
            "bibliothe",
            "library",
            "biblioteca",
        ),
    ),
    # ── Sport ──
    (
        "🏊",
        (
            "^swim",
            "schwimm",
            "natacion",
            "natation",
            "^zwem",
            "piscine",
            "piscina",
            "hallenbad",
            "freibad",
        ),
    ),
    ("🏓", ("tischtennis", "table tennis", "ping pong", "pingpong")),
    ("🏀", ("basket", "baloncesto")),
    ("🏐", ("volley",)),
    ("🤾", ("handball", "balonmano")),
    ("🏒", ("hockey",)),
    ("🎾", ("tennis", "=tenis")),
    ("⚽", ("football", "soccer", "fussball", "futbol", "voetbal", "=foot")),
    ("🩰", ("ballet", "ballett")),
    (
        "💃",
        (
            "^dance",
            "^dancing",
            "^tanz",
            "danza",
            "danse",
            "=dans",
            "dansen",
            "=baile",
            "bailar",
        ),
    ),
    (
        "🥋",
        (
            "karate",
            "=judo",
            "taekwondo",
            "kung fu",
            "aikido",
            "kampfsport",
            "jiu jitsu",
        ),
    ),
    ("🤸", ("turnen", "gymnast", "gimnasia", "^acrobat", "^akrobat")),
    ("🧘", ("=yoga",)),
    ("🏋️", ("=gym", "fitness", "gimnasio", "workout", "^sportschool")),
    (
        "🚴",
        (
            "=bike",
            "biking",
            "cycling",
            "fahrrad",
            "radfahr",
            "=radeln",
            "bicicleta",
            "=velo",
            "=fiets",
            "fietsen",
        ),
    ),
    (
        "🏃",
        (
            "running",
            "=run",
            "=jog",
            "jogging",
            "joggen",
            "=laufen",
            "correr",
            "=courir",
            "hardlopen",
        ),
    ),
    ("⛷️", ("=ski", "skiing", "skifahr", "=esqui", "=skien", "skikurs", "snowboard")),
    ("⛸️", ("eislauf", "schlittschuh", "skating", "patinaje", "patinage", "schaatsen")),
    # ── Music + making things ──
    ("🎹", ("piano", "klavier", "keyboard")),
    ("🎸", ("guitar", "gitarre", "guitarra", "guitare", "gitaar")),
    ("🎻", ("violin", "geige", "violon", "viool", "=cello")),
    ("🥁", ("=drums", "drumming", "schlagzeug", "trommel", "drummen")),
    ("🪈", ("flote", "floete", "=flute", "flauta", "=fluit")),
    (
        "🎤",
        (
            "=choir",
            "=chor",
            "singen",
            "singing",
            "=sing",
            "=coro",
            "=cantar",
            "chorale",
            "=koor",
            "zingen",
        ),
    ),
    ("🎵", ("music", "musik", "musica", "musique", "muziek", "orchest")),
    (
        "🎨",
        (
            "basteln",
            "^craft",
            "painting",
            "^malen",
            "=paint",
            "pintar",
            "peindre",
            "dessin",
            "^schilderen",
            "knutsel",
            "tekenen",
            "^zeichnen",
            "dibujo",
            "dibujar",
            "manualidad",
            "bricolage",
        ),
    ),
    ("🎭", ("theater", "theatre", "teatro", "toneel", "drama")),
    # ── School + outings ──
    (
        "🎒",
        (
            "school",
            "schule",
            "colegio",
            "escuela",
            "=ecole",
            "=kita",
            "kindergarten",
            "=kiga",
            "creche",
            "guarderia",
            "opvang",
        ),
    ),
    ("🎬", ("movie", "=film", "=films", "=kino", "=cine", "cinema", "bioscoop")),
    ("🦁", ("=zoo", "^zoolog", "tierpark", "dierentuin")),
    ("🛝", ("spielplatz", "playground", "=parque", "=parc", "=park", "speeltuin")),
    (
        "🧩",
        (
            "playdate",
            "=spiel",
            "=spielen",
            "^spielgruppe",
            "^spieltreff",
            "^spielenach",
            "=play",
            "=jugar",
            "=jouer",
            "=spelen",
        ),
    ),
    (
        "👵",
        (
            "=oma",
            "=opa",
            "=omi",
            "=opi",
            "grosseltern",
            "grandma",
            "grandpa",
            "granny",
            "grandparent",
            "=gran",
            "abuel",
            "grand mere",
            "grand pere",
            "grands parents",
            "=mamie",
            "=papy",
            "grootouders",
        ),
    ),
    (
        "⛪",
        (
            "=church",
            "=churches",
            "kirche",
            "=iglesia",
            "eglise",
            "=kerk",
            "=misa",
            "gottesdienst",
        ),
    ),
    # ── Travel + work ──
    (
        "✈️",
        (
            "flight",
            "^flug",
            "airport",
            "=vuelo",
            "aeropuerto",
            "=vol",
            "aeroport",
            "vlucht",
            "vliegveld",
        ),
    ),
    (
        "🏖️",
        (
            "vacation",
            "holiday",
            "urlaub",
            "vacacion",
            "vacances",
            "vakantie",
            "^ferien",
            "=beach",
            "=strand",
            "=playa",
            "=plage",
        ),
    ),
    ("🧳", ("=trip", "^reise", "=viaje", "voyage", "=reis", "^reizen")),
    ("🚌", ("=bus", "schulbus", "=autobus")),
    ("🚗", ("abholen", "pick up", "pickup", "=recoger", "aller chercher", "=ophalen")),
    (
        "💼",
        (
            "=work",
            "=arbeit",
            "=arbeiten",
            "^buro",
            "^bureau",
            "=office",
            "trabajo",
            "travail",
            "=werk",
            "^meeting",
            "besprechung",
            "reunion",
            "vergadering",
        ),
    ),
    (
        "📞",
        (
            "=call",
            "facetime",
            "videocall",
            "anruf",
            "telefon",
            "llamar",
            "llamada",
            "=appel",
            "appeler",
            "=bellen",
        ),
    ),
    (
        "🏠",
        (
            "=home",
            "=heim",
            "heimweg",
            "zuhause",
            "nach hause",
            "=maison",
            "=casa",
            "=huis",
            "naar huis",
        ),
    ),
)


_Compiled = tuple[tuple[str, tuple[str, ...]], ...]


def _needle(keyword: str, *, by_length: bool) -> str:
    """Compile a keyword (see the module doc) into the string to look for
    in ``" " + " ".join(words) + " "``. ``by_length=False`` reads
    unmarked keywords the SPA's way: whole word up to 3 letters, else
    the start of a word."""
    mark, word = (keyword[0], keyword[1:]) if keyword[0] in "=^*" else ("", keyword)
    if not mark:
        if len(word) <= 3:
            mark = "="
        elif len(word) == 4 or not by_length or " " in word:
            mark = "^"
        else:
            mark = "*"
    if mark == "=":
        return f" {word} "
    if mark == "^":
        return f" {word}"
    return word


def _compile(table: _Table, *, by_length: bool) -> _Compiled:
    return tuple(
        (emoji, tuple(_needle(k, by_length=by_length) for k in keywords))
        for emoji, keywords in table
    )


_GENERAL = _compile(GENERAL_KEYWORDS, by_length=True)
_LESSONS = _compile(LESSON_KEYWORDS, by_length=False)

_LETTERS = re.compile(r"[^\W\d_]+")


def normalized_words(text: str) -> list[str]:
    """``text`` as lowercase words without diacritics ("Zähne putzen!" →
    ``["zahne", "putzen"]``); digits and punctuation separate words."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _LETTERS.findall(plain)


def _search(hay: str, table: _Compiled) -> str | None:
    for emoji, needles in table:
        if any(n in hay for n in needles):
            return emoji
    return None


def suggest_icon(title: str | None, *, school: bool = False) -> str | None:
    """An emoji for ``title`` from the keyword tables — ``None`` when
    nothing matches. ``school=True`` (a timetable slot) tries the
    subject table first, then the everyday one; anything else uses only
    the everyday one."""
    words = normalized_words(title or "")
    if not words:
        return None
    hay = f" {' '.join(words)} "
    # The subject table only for lessons: its SPA-style word starts
    # ("reli", "german", "break") misfire on everyday event titles.
    tables = (_LESSONS, _GENERAL) if school else (_GENERAL,)
    for table in tables:
        found = _search(hay, table)
        if found is not None:
            return found
    return None


def title_icon(title: str | None, *, school: bool = False) -> str:
    """The emoji ``title`` starts with, else a keyword guess, else
    :data:`FALLBACK_ICON`."""
    text = (title or "").strip()
    return leading_emoji(text) or suggest_icon(text, school=school) or FALLBACK_ICON
