"""Parse a pasted decklist into (quantity, card name) pairs.

Handles the formats people actually paste: Arena/MTGO exports, Moxfield,
Archidekt, EDHREC and hand-typed lists. Section headers, comments and blank
lines are dropped.
"""

import re

# "4 Lightning Bolt", "4x Lightning Bolt", "Lightning Bolt x4", "Lightning Bolt"
_LEADING_QTY = re.compile(r"^(\d{1,3})\s*[xX]?\s+(.*)$")
_TRAILING_QTY = re.compile(r"^(.*?)\s+[xX]\s*(\d{1,3})$")

# Trailing printing hints: "(MH2) 401", "[MH2]", "<MH2>", "#401", "(MH2)"
_SET_SUFFIX = re.compile(r"\s*[\(\[<]([A-Za-z0-9_]{2,6})[\)\]>]\s*(?:[A-Za-z]?\d{1,4}[a-z★]?)?\s*$")
_COLLECTOR_SUFFIX = re.compile(r"\s*#\s*\d{1,4}[a-z]?\s*$")

# Arena adds these; Moxfield/Archidekt add category headings.
_HEADERS = {
    "deck", "sideboard", "commander", "companion", "maybeboard", "considering",
    "tokens", "token", "about", "mainboard", "main", "side", "creature",
    "creatures", "instant", "instants", "sorcery", "sorceries", "artifact",
    "artifacts", "enchantment", "enchantments", "planeswalker", "planeswalkers",
    "land", "lands", "battle", "battles", "other", "outside the deck",
}

_FOIL_MARKERS = re.compile(r"\s*\*(?:F|E)\*\s*$", re.IGNORECASE)


def _strip_printing_hints(name):
    """Remove set codes and collector numbers, keeping any hint we stripped."""
    hint = None
    name = _FOIL_MARKERS.sub("", name).strip()
    name = _COLLECTOR_SUFFIX.sub("", name).strip()
    match = _SET_SUFFIX.search(name)
    if match:
        hint = match.group(1).upper()
        name = name[: match.start()].strip()
    return name, hint


def parse(text):
    """Return (entries, skipped_lines).

    Each entry is {"qty": int, "name": str, "set_hint": str|None, "line": str}.
    Duplicate names are merged, summing quantities.
    """
    entries = []
    skipped = []
    by_name = {}

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("//") or line.startswith("#"):
            continue
        # Moxfield/Archidekt headings look like "Creatures (24)".
        heading = re.sub(r"\s*\(\d+\)\s*$", "", line).strip().rstrip(":").lower()
        if heading in _HEADERS:
            continue

        qty = 1
        name = line
        match = _LEADING_QTY.match(line)
        if match:
            qty, name = int(match.group(1)), match.group(2)
        else:
            match = _TRAILING_QTY.match(line)
            if match:
                name, qty = match.group(1), int(match.group(2))

        name, set_hint = _strip_printing_hints(name.strip())
        if not name or not re.search(r"[A-Za-z]", name):
            skipped.append(raw_line)
            continue

        key = name.lower()
        if key in by_name:
            by_name[key]["qty"] += qty
            continue
        entry = {"qty": max(1, qty), "name": name, "set_hint": set_hint, "line": raw_line}
        by_name[key] = entry
        entries.append(entry)

    return entries, skipped
