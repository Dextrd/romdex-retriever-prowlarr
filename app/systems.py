"""The systems a release's name may say it's for, so a game is looked for on its own system and not another's.

Romdex's own list of systems (KnownSystems in Romdex.Core), with the names releases go by and the extensions of files
only one system has, such as Switch's .nsp, which release names often carry in brackets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORD = re.compile(r"[a-z0-9]+")


def word_list(name: str) -> list[str]:
    return _WORD.findall(name.lower())


@dataclass(frozen=True)
class System:
    short: str
    dats: tuple[str, ...]
    also: tuple[str, ...] = ()
    # Extensions only this system's games come as.
    extensions: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """Its DAT's name without the maker, such as Nintendo Entertainment System."""
        return self.dats[0].split(" - ", 1)[-1]

    def phrases(self) -> list[tuple[str, ...]]:
        names = [self.short, *self.also, *self.extensions, *(d.split(" - ", 1)[-1] for d in self.dats)]
        return [tuple(word_list(n)) for n in names if word_list(n)]


def _s(short: str, dats: list[str], also: list[str] | None = None, ext: list[str] | None = None) -> System:
    return System(short, tuple(dats), tuple(also or ()), tuple(ext or ()))


# Short names that are also everyday words, or too often part of other names, are left out (Genesis's GEN, Saturn's
# SAT, Switch's NS, the first PlayStation's PS).
ALL = [
    _s("NES", ["Nintendo - Nintendo Entertainment System"], ["Famicom"], ["unf"]),
    _s("FDS", ["Nintendo - Family Computer Disk System"], ["Famicom Disk System"]),
    _s("SNES", ["Nintendo - Super Nintendo Entertainment System"], ["Super NES", "SFC", "Super Famicom"], ["smc"]),
    _s("N64", ["Nintendo - Nintendo 64"], ["Nintendo 64"], ["z64", "v64"]),
    _s("64DD", ["Nintendo - Nintendo 64DD"]),
    _s("GC", ["Nintendo - GameCube"], ["GCN", "NGC", "GameCube"]),
    _s("Wii", ["Nintendo - Wii"], [], ["wbfs"]),
    _s("Wii U", ["Nintendo - Wii U"], ["WiiU"], ["wua", "wux", "rpx"]),
    _s("Switch", ["Nintendo - Nintendo Switch", "Nintendo - Switch"], ["Nintendo Switch"], ["nsp", "xci", "nsz", "xcz"]),
    _s("GB", ["Nintendo - Game Boy"], ["Game Boy"]),
    _s("GBC", ["Nintendo - Game Boy Color"], ["Game Boy Color", "Game Boy Colour"]),
    _s("GBA", ["Nintendo - Game Boy Advance"], ["Game Boy Advance"]),
    _s("VB", ["Nintendo - Virtual Boy"], ["Virtual Boy"]),
    _s("DS", ["Nintendo - Nintendo DS"], ["NDS", "Nintendo DS"]),
    _s("DSi", ["Nintendo - Nintendo DSi"], ["Nintendo DSi"]),
    _s("3DS", ["Nintendo - Nintendo 3DS", "Nintendo - New Nintendo 3DS"], ["N3DS", "Nintendo 3DS", "New Nintendo 3DS"],
       ["cia", "3dsx"]),
    _s("SMS", ["Sega - Master System - Mark III", "Sega - Master System"], ["Master System", "Sega Master System"]),
    _s("GG", ["Sega - Game Gear"], ["Game Gear"]),
    _s("MD", ["Sega - Mega Drive - Genesis"], ["SMD", "Genesis", "Mega Drive", "Sega Genesis"]),
    _s("32X", ["Sega - 32X"], ["Sega 32X"]),
    _s("MCD", ["Sega - Mega CD & Sega CD", "Sega - Mega-CD - Sega CD"], ["SCD", "Sega CD", "Mega-CD", "Mega CD"]),
    _s("Saturn", ["Sega - Saturn"], ["Sega Saturn"]),
    _s("DC", ["Sega - Dreamcast"], ["Dreamcast"]),
    _s("PS1", ["Sony - PlayStation"], ["PSX", "PlayStation", "PlayStation 1"]),
    _s("PS2", ["Sony - PlayStation 2"], ["PlayStation 2"]),
    _s("PS3", ["Sony - PlayStation 3"], ["PlayStation 3"]),
    _s("PS4", ["Sony - PlayStation 4"], ["PlayStation 4"]),
    _s("PS5", ["Sony - PlayStation 5"], ["PlayStation 5"]),
    _s("PSP", ["Sony - PlayStation Portable"], ["PlayStation Portable"]),
    _s("PSV", ["Sony - PlayStation Vita"], ["Vita", "PS Vita", "PlayStation Vita"], ["vpk"]),
    _s("Xbox", ["Microsoft - Xbox"]),
    _s("X360", ["Microsoft - Xbox 360"], ["Xbox 360"]),
    _s("XONE", ["Microsoft - Xbox One"], ["XB1", "Xbox One"]),
    _s("XSX", ["Microsoft - Xbox Series X", "Microsoft - Xbox Series X|S", "Microsoft - Xbox Series"],
       ["Xbox Series X", "Xbox Series"]),
    _s("PCE", ["NEC - PC Engine - TurboGrafx-16"], ["TG16", "TG-16", "TurboGrafx-16", "TurboGrafx", "PC Engine"]),
    _s("PCECD", ["NEC - PC Engine CD & TurboGrafx CD", "NEC - PC Engine CD - TurboGrafx-CD"],
       ["TurboGrafx-CD", "PC Engine CD", "TG-CD"]),
    _s("PC-FX", ["NEC - PC-FX & PC-FXGA", "NEC - PC-FX"]),
    _s("NGCD", ["SNK - Neo Geo CD"], ["Neo Geo CD"]),
    _s("NGP", ["SNK - NeoGeo Pocket"], ["Neo Geo Pocket"]),
    _s("NGPC", ["SNK - NeoGeo Pocket Color"], ["Neo Geo Pocket Color"]),
    _s("WS", ["Bandai - WonderSwan"], ["WonderSwan"]),
    _s("WSC", ["Bandai - WonderSwan Color"], ["WonderSwan Color"]),
    _s("2600", ["Atari - 2600"], ["Atari 2600", "VCS"]),
    _s("7800", ["Atari - 7800"], ["Atari 7800"]),
    _s("Lynx", ["Atari - Lynx"], ["Atari Lynx"]),
    _s("Jaguar", ["Atari - Jaguar"], ["Atari Jaguar"]),
    _s("CV", ["Coleco - ColecoVision"], ["ColecoVision"]),
    _s("3DO", ["Panasonic - 3DO Interactive Multiplayer"], ["3DO"]),
]


def _key(name: str) -> str:
    return "".join(word_list(name.split(" (")[0]))


_BY_DAT = {_key(d): s for s in ALL for d in s.dats}
# Every way a system is written, longest first, so Super Nintendo Entertainment System isn't read as the NES.
_PHRASES = sorted(((p, s) for s in ALL for p in s.phrases()), key=lambda ps: -len(ps[0]))


def find(dat_name: str) -> System | None:
    """The system a DAT is for, by its name, without what it adds in brackets, such as (Headered)."""
    return _BY_DAT.get(_key(dat_name))


def mentions(name: str, ignore: set[str] = frozenset(), extra: list[tuple[str, System | None]] = ()) -> tuple[set[System | None], set[str]]:
    """The systems a release's name says it's for, and the words that said so. Ways of writing them made only of words in
    `ignore` don't count, as they're the game's own words, such as Famicom in Famicom Wars; `extra` are more ways, such
    as an unknown system's name, for which None is returned."""
    words = word_list(name)
    phrases = sorted([*_PHRASES, *((tuple(word_list(n)), s) for n, s in extra if word_list(n))], key=lambda ps: -len(ps[0]))
    found: set[System | None] = set()
    used: set[str] = set()
    i = 0
    while i < len(words):
        for phrase, system in phrases:
            if tuple(words[i:i + len(phrase)]) == phrase and not set(phrase) <= ignore:
                found.add(system)
                used |= set(phrase)
                i += len(phrase)
                break
        else:
            i += 1
    return found, used
