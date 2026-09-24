#!/usr/bin/env python3
"""Emoji for helm comms — the piece the predecessors never ported (PRD addendum
2026-07-19): shortcode expansion at post time on every input surface (CLI,
web, TUI), UTF-8 purity end-to-end, and a degrade path (demojize) so a
terminal that cannot encode an emoji shows the shortcode text instead of
crashing. Agents are encouraged to use them like anyone else in the room.

MAP is a module constant (no external data files) — the ~140 most-used
shortcodes, gemoji-compatible names. Width helpers cover the TUI's wide-char
handling (east-asian W/F ≈ 2 columns; VS16/ZWJ/combining ≈ 0) — an
approximation, which is the honest ceiling terminals themselves live at.

Import-safe, stdlib-only.
"""
import re
import unicodedata

MAP = {
    # faces
    "smile": "😄", "grin": "😁", "joy": "😂", "rofl": "🤣", "slight_smile": "🙂",
    "upside_down": "🙃", "wink": "😉", "blush": "😊", "innocent": "😇",
    "heart_eyes": "😍", "star_struck": "🤩", "yum": "😋", "zany": "🤪",
    "hugs": "🤗", "thinking": "🤔", "shush": "🤫", "neutral": "😐",
    "expressionless": "😑", "smirk": "😏", "unamused": "😒", "roll_eyes": "🙄",
    "grimacing": "😬", "relieved": "😌", "pensive": "😔", "sleepy": "😪",
    "sleeping": "😴", "zzz": "💤", "mask": "😷", "dizzy_face": "😵",
    "exploding_head": "🤯", "cowboy": "🤠", "partying": "🥳", "sunglasses": "😎",
    "nerd": "🤓", "monocle": "🧐", "confused": "😕", "worried": "😟",
    "open_mouth": "😮", "astonished": "😲", "flushed": "😳", "pleading": "🥺",
    "cry": "😢", "sob": "😭", "scream": "😱", "disappointed": "😞",
    "sweat": "😓", "sweat_smile": "😅", "weary": "😩", "tired": "😫",
    "yawning": "🥱", "triumph": "😤", "angry": "😠", "rage": "😡",
    "skull": "💀", "clown": "🤡", "ghost": "👻", "alien": "👽", "robot": "🤖",
    # gestures + body
    "wave": "👋", "raised_hand": "✋", "ok_hand": "👌", "v": "✌️",
    "crossed_fingers": "🤞", "metal": "🤘", "call_me": "🤙",
    "point_left": "👈", "point_right": "👉", "point_up": "👆", "point_down": "👇",
    "thumbsup": "👍", "+1": "👍", "thumbsdown": "👎", "-1": "👎",
    "fist": "✊", "clap": "👏", "raised_hands": "🙌", "handshake": "🤝",
    "pray": "🙏", "salute": "🫡", "muscle": "💪", "writing_hand": "✍️",
    "eyes": "👀", "brain": "🧠",
    # hearts + symbols
    "heart": "❤️", "orange_heart": "🧡", "yellow_heart": "💛",
    "green_heart": "💚", "blue_heart": "💙", "purple_heart": "💜",
    "black_heart": "🖤", "white_heart": "🤍", "broken_heart": "💔",
    "two_hearts": "💕", "sparkling_heart": "💖", "100": "💯", "boom": "💥",
    "dash": "💨", "sweat_drops": "💦", "fire": "🔥", "star": "⭐",
    "star2": "🌟", "sparkles": "✨", "zap": "⚡", "sun": "☀️", "moon": "🌙",
    "rainbow": "🌈", "snowflake": "❄️",
    # objects + dev life
    "rocket": "🚀", "ship": "🚢", "anchor": "⚓", "package": "📦",
    "gift": "🎁", "tada": "🎉", "confetti": "🎊", "balloon": "🎈",
    "trophy": "🏆", "medal": "🏅", "crown": "👑", "gem": "💎",
    "moneybag": "💰", "chart": "📈", "chart_down": "📉", "clipboard": "📋",
    "memo": "📝", "pencil": "✏️", "book": "📖", "books": "📚", "mag": "🔍",
    "bulb": "💡", "wrench": "🔧", "hammer": "🔨", "tools": "🛠️",
    "gear": "⚙️", "link": "🔗", "paperclip": "📎", "scissors": "✂️",
    "lock": "🔒", "unlock": "🔓", "key": "🔑", "bell": "🔔", "bug": "🐛",
    "butterfly": "🦋", "turtle": "🐢", "fox": "🦊", "owl": "🦉",
    "dragon": "🐉", "unicorn": "🦄", "whale": "🐳", "octopus": "🐙",
    # status + signals
    "check": "✅", "white_check_mark": "✅", "heavy_check_mark": "✔️",
    "x": "❌", "warning": "⚠️", "no_entry": "⛔", "stop_sign": "🛑",
    "sos": "🆘", "question": "❓", "exclamation": "❗",
    "red_circle": "🔴", "green_circle": "🟢", "yellow_circle": "🟡",
    "blue_circle": "🔵", "hourglass": "⌛", "stopwatch": "⏱️",
    "alarm_clock": "⏰", "calendar": "📅", "pushpin": "📌", "dart": "🎯",
    "flag": "🚩", "checkered_flag": "🏁", "white_flag": "🏳️", "eyes_up": "🔭",
}

# emoji -> canonical shortcode (first name in MAP wins for aliases)
_REVERSE = {}
for _k, _v in MAP.items():
    _REVERSE.setdefault(_v, _k)

_SC = re.compile(r":([a-z0-9_+-]{1,32}):")


def expand(text):
    """:fire: -> 🔥 everywhere in `text`; unknown shortcodes pass untouched
    (they might be prose like `:this:`). Applied ONCE at post time — stored
    text is the expanded truth on every surface."""
    return _SC.sub(lambda m: MAP.get(m.group(1), m.group(0)), text or "")


def demojize(text):
    """The degrade path: every known emoji back to :shortcode: — what a
    surface renders when its encoding cannot carry the glyph (never crash on
    an emoji law). Longest emoji first so ZWJ/VS16 forms match whole."""
    for e in sorted(_REVERSE, key=len, reverse=True):
        if e in text:
            text = text.replace(e, ":" + _REVERSE[e] + ":")
    return text


def ch_width(ch):
    """One character's terminal column estimate: combining/VS16/ZWJ 0,
    east-asian Wide/Fullwidth 2, else 1. Emoji land in W."""
    o = ord(ch)
    if o in (0xFE0F, 0x200D) or unicodedata.combining(ch):
        return 0
    if 0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF:
        return 2
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def width(s):
    return sum(ch_width(ch) for ch in s or "")


def clip(s, w):
    """Width-aware prefix of `s` fitting in `w` columns (never splits a wide
    char across the boundary)."""
    out = []
    used = 0
    for ch in s or "":
        cw = ch_width(ch)
        if used + cw > w:
            break
        out.append(ch)
        used += cw
    return "".join(out)


def wrap(s, w):
    """Width-aware wrap into lines of at most `w` columns; word-preferring
    (breaks at the last space when one exists), never returns []."""
    if w < 2:
        return [s or ""]
    lines = []
    rest = s or ""
    while width(rest) > w:
        head = clip(rest, w)
        cut = head.rfind(" ")
        if cut > w // 3:
            head = head[:cut]
            rest = rest[cut + 1:]
        else:
            rest = rest[len(head):]
        lines.append(head)
    lines.append(rest)
    return lines
