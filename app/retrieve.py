"""Finding a game's dumps among the torrents Prowlarr's indexers offer, and fetching them with qBittorrent.

A torrent may hold one dump or a whole system's, so a dump is fetched by downloading just its files. Before offering a
torrent, its file list is read from the .torrent file, and it's offered only when it has a file named as one of the
game's dumps is in the DAT: its own files by name, such as a disc's cue sheet and tracks, or an archive or a CHD named
after the dump. Torrents that come only as a magnet link can't be checked until qBittorrent has their file list, so
they rank lower and are checked when fetched. Romdex works out what arrived by its checksums either way.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

from . import reflinks, systems
from . import torrent as torrents
from .clients import Prowlarr, QBittorrent, without_key
from .contract import Candidate, Job, Target, TargetDump
from .matching import similarity, words

AFTER = ["keep", "remove"]
POLL_SECONDS = 2.0
ADD_WAIT_SECONDS = 60.0

_CHECKED = 8  # torrents named like the game whose file lists are read per search
_SETS = 3  # and torrents named for its system, such as a whole set, which don't name the game
# What a dump may come as besides its own files, which Romdex reads inside.
CONTAINERS = {".zip", ".7z", ".chd", ".rvz"}
# Never a game: what releases bring along.
EXTRAS = {".txt", ".nfo", ".jpg", ".jpeg", ".png", ".gif", ".url", ".sfv", ".md5", ".sha1", ".dat", ".xml", ".htm",
          ".html", ".pdf", ".exe", ".torrent"}


# ---- What's in a torrent ----

def stem(path: str) -> str:
    return PurePosixPath(path).stem


def suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def dumps(target: Target) -> list[TargetDump]:
    """The dumps wanted: the one asked for, or any, the preferred first."""
    return [d for d in target.dumps if not target.dump_name or d.name == target.dump_name]


def match_dump(dump: TargetDump, files: list[torrents.TorrentFile], min_score: float,
               foreign: frozenset[str] = frozenset()) -> tuple[list[torrents.TorrentFile], float] | None:
    """The dump's files in the torrent, and how sure that is: its own files by name, an archive or a CHD named after
    it, or failing those the file named most like it, but for files of other systems (`foreign`, such as .nsp)."""
    by_name: dict[str, list[torrents.TorrentFile]] = {}
    for f in files:
        by_name.setdefault(PurePosixPath(f.path).name.lower(), []).append(f)
    own = [f.name.lower() for f in dump.files]
    if own and all(n in by_name for n in own):
        picked = [by_name[n][0] for n in own]
        sizes = {f.name.lower(): f.size for f in dump.files}
        same = all(f.size == sizes[PurePosixPath(f.path).name.lower()] for f in picked)
        return picked, 1.0 if same else 0.9
    name = dump.name.lower()
    if packed := [f for f in files if stem(f.path).lower() == name and suffix(f.path) in CONTAINERS]:
        return [packed[0]], 0.95
    games = [f for f in files if suffix(f.path) not in EXTRAS and suffix(f.path) not in foreign]
    best = max(games, key=lambda f: similarity(dump.name, stem(f.path)), default=None)
    if best is None or (score := similarity(dump.name, stem(best.path)) * 0.9) < min_score:
        return None
    # A cue sheet or gdi comes with the tracks in its folder.
    if suffix(best.path) in (".cue", ".gdi"):
        folder = PurePosixPath(best.path).parent
        return [f for f in games if PurePosixPath(f.path).parent == folder], score
    return [best], score


def match(target: Target, files: list[torrents.TorrentFile], min_score: float) -> tuple[TargetDump, list[torrents.TorrentFile], float] | None:
    """The best of the game's dumps the torrent has, its files, and how sure that is; the preferred dump between
    equally good ones."""
    found = None
    foreign = _foreign(target)
    for i, dump in enumerate(dumps(target)):
        if (matched := match_dump(dump, files, min_score, foreign)) is None:
            continue
        score = matched[1] - min(i, 5) * 0.01
        if found is None or score > found[2]:
            found = (dump, matched[0], score)
    return found


# ---- Search ----

# How a release's name counts for the game, by the system it says it's for.
ON_SYSTEM, NO_SYSTEM, OTHER_SYSTEM = 1.0, 0.9, 0.5
# Words in releases' names that don't say which game it is: who dumped it, its region and the like.
_TAGS = {"rom", "roms", "romset", "nointro", "no", "intro", "redump", "tosec", "headered", "headerless", "verified",
         "set", "complete", "full", "usa", "us", "europe", "eu", "eur", "pal", "ntsc", "japan", "jp", "jpn", "jap",
         "world", "en", "multi", "rev", "proper", "repack", "cart", "cartridge"}
_YEAR = re.compile(r"(19|20)\d\d$")


def _own_name(target: Target) -> str:
    """The system's name without its maker or what's in brackets: Nintendo Entertainment System."""
    return target.system.split(" (")[0].split(" - ", 1)[-1]


def _foreign(target: Target) -> frozenset[str]:
    """Extensions of other systems' games, such as Switch's .nsp for a NES game."""
    own = systems.find(target.system)
    return frozenset(f".{e}" for s in systems.ALL if s is not own for e in s.extensions)


def system_fit(target: Target, name: str) -> tuple[float, set[str]]:
    """Whether a release's name says it's for the game's system, another, or none; and the words that said so. A name
    that has the game's system counts as on it, whatever else it names, as packs of several systems do."""
    own = systems.find(target.system)
    found, used = systems.mentions(name, ignore=words(target.title), extra=[] if own else [(_own_name(target), None)])
    if own in found:
        return ON_SYSTEM, used
    return (OTHER_SYSTEM if found else NO_SYSTEM), used


def name_fit(target: Target, name: str) -> float:
    """How alike a release's name is to the game's, from 0 to 1, leaving out the words that name a system, a region or
    who dumped it: a sequel or another game of the series has words of its own, which count against it."""
    title = words(target.title)
    tags = _TAGS | {w for d in dumps(target) for part in re.findall(r"\(([^)]*)\)", d.name) for w in words(part)
                    if not w.isdigit()}
    _, used = system_fit(target, name)
    rest = {w for w in words(name) if w in title or not (w in used or w in tags or _YEAR.match(w))}
    return len(title & rest) / len(title | rest) if title and rest else 0.0


def title_score(target: Target, name: str) -> float:
    """How well a torrent's name fits the game before its files are looked at: as the game or one of its dumps, on its
    system."""
    fit, _ = system_fit(target, name)
    return fit * max([name_fit(target, name), *(similarity(d.name, name) for d in dumps(target))])


def query(title: str) -> str:
    """What's searched for: the game's words, as indexers want every word searched for."""
    return " ".join(re.findall(r"\w+", title))


def queries(target: Target) -> list[str]:
    """The game's name; the game's name with its system's, which finds it among others' games of the same name; and
    the system's, which finds torrents of a whole set."""
    title = query(target.title)
    own = systems.find(target.system)
    found = [title, f"{title} {query(own.short)}" if own else "", query(own.name if own else _own_name(target))]
    return list(dict.fromkeys(q for q in found if q))


def search(target: Target, limit: int, settings: dict[str, Any]) -> list[Candidate]:
    min_score = float(settings.get("minScore", 0.6))
    min_seeders = int(settings.get("minSeeders", 1))
    with Prowlarr(settings) as prowlarr, ThreadPoolExecutor(max_workers=4) as pool:
        first, *more = queries(target)
        others = [pool.submit(prowlarr.search, q) for q in more]
        results = list(prowlarr.search(first))
        for other in others:
            # The game's own search has to work; the others only add to it.
            try:
                results += other.result()
            except Exception:
                pass
        releases: dict[str, dict[str, Any]] = {}
        for release in results:
            if (release.get("seeders") or 0) >= min_seeders:
                releases.setdefault(release.get("infoHash") or release.get("guid") or release["title"], release)
        ranked = sorted(releases.values(), key=lambda r: (title_score(target, r["title"]), r.get("seeders") or 0), reverse=True)
        named = ranked[:_CHECKED]
        # A set's name doesn't name the game, so those named for its system are looked in too, the best-seeded first.
        sets = sorted((r for r in ranked[_CHECKED:] if system_fit(target, r["title"])[0] == ON_SYSTEM),
                      key=lambda r: r.get("seeders") or 0, reverse=True)[:_SETS]
        checked = list(pool.map(lambda r: (r, _files(prowlarr, r)), named + sets))

    candidates = []
    for release, found in checked:
        if (candidate := _candidate(target, release, found, min_score)) and (candidate.score or 0) >= min_score:
            candidates.append(candidate)
    # Between equally good matches, the better-seeded.
    candidates.sort(key=lambda c: (c.score or 0, _seeders(c)), reverse=True)
    return candidates[:limit]


def _seeders(candidate: Candidate) -> int:
    return int(m.group(1)) if (m := re.search(r"\((\d+) seeders\)", candidate.source)) else 0


def _files(prowlarr: Prowlarr, release: dict[str, Any]) -> tuple[str, list[torrents.TorrentFile]] | str | None:
    """The release's info hash and files, its magnet link when that's all there is, or None when it can't be had."""
    link = release.get("downloadUrl")
    try:
        got = prowlarr.torrent(link) if link else release.get("magnetUrl")
        if isinstance(got, bytes):
            return torrents.read(got)
        return got
    except Exception:
        return release.get("magnetUrl")


def _formats(files: list[torrents.TorrentFile]) -> str | None:
    return ",".join(sorted({suffix(f.path).lstrip(".") for f in files if suffix(f.path)})) or None


def _candidate(target: Target, release: dict[str, Any], found: Any, min_score: float) -> Candidate | None:
    name = release["title"]
    common = {"source": f"{release.get('indexer') or 'Prowlarr'} ({release.get('seeders') or 0} seeders)",
              "url": release.get("infoUrl") or release.get("commentUrl")}
    if isinstance(found, tuple):
        info_hash, files = found
        if (matched := match(target, files, min_score)) is None:
            return None
        dump, picked, score = matched
        return Candidate(
            candidate_id=json.dumps({"h": info_hash, "t": without_key(release["downloadUrl"]), "d": dump.name,
                                     "f": [[f.index, f.path] for f in picked]}),
            name=dump.name, dump_name=dump.name, format=_formats(picked), size=sum(f.size for f in picked),
            score=round(score, 3), notes=_notes(name, picked), **common)

    # A magnet link only: offered on the name alone, and checked once qBittorrent has the file list.
    magnet = found if isinstance(found, str) else release.get("magnetUrl")
    info_hash = release.get("infoHash") or (torrents.magnet_hash(magnet) if magnet else None)
    if not magnet or not info_hash:
        return None
    return Candidate(candidate_id=json.dumps({"h": info_hash.lower(), "m": magnet, "d": None, "f": None}), name=name,
                     score=round(0.8 * title_score(target, name), 3),
                     notes=f"From the torrent {name}. It's a magnet link, so which of its files are the game is "
                           f"checked once qBittorrent has its file list.", **common)


def _notes(release: str, picked: list[torrents.TorrentFile]) -> str:
    shown = "\n".join(f"- {f.path}" for f in picked[:20]) + ("\n…" if len(picked) > 20 else "")
    return f"From the torrent {release}, just these files:\n{shown}"


# ---- Fetch ----

def fetch(job: Job, candidate_id: str, target: Target | None, directory: Path, settings: dict[str, Any]) -> list[dict[str, Any]]:
    chosen = json.loads(candidate_id)
    info_hash: str = chosen["h"]
    with QBittorrent(settings) as qb:
        added = False
        mine = f"romdex-{job.id}"
        try:
            existing = qb.info(info_hash)
            if existing is None:
                if chosen.get("t"):
                    with Prowlarr(settings) as prowlarr:
                        source = prowlarr.torrent(chosen["t"])
                else:
                    source = chosen["m"]
                qb.add(source, str(settings.get("savePath") or "/downloads/romdex"), str(settings.get("category") or "romdex"),
                       f"romdex,{mine}")
                added = True

            listed = _wait_for_files(job, qb, info_hash)
            # Each request using the torrent marks it, so the others know not to remove it from under it. It's marked
            # again after adding, as qBittorrent ignores adding a torrent it has, along with its tags, when two
            # requests add it at once; and once it's listed, as it's added in the background.
            qb.add_tags(info_hash, mine)
            dump_name, picked = _pick(chosen, listed, target, float(settings.get("minScore", 0.6)))
            wanted = {index for index, _ in picked}
            if added:
                qb.priority(info_hash, [f["index"] for f in listed if f["index"] not in wanted], 0)
            # A torrent that was already there keeps the files it had; the requested ones are added to it.
            qb.priority(info_hash, [f["index"] for f in listed if f["index"] in wanted and f["priority"] == 0], 1)
            qb.start(info_hash)

            _wait_for_download(job, qb, info_hash, wanted)
            folder = _local_folder(settings, qb.info(info_hash) or {})
            # A dump of several files, such as a disc's cue sheet and tracks, stays together in a folder of its own.
            into = directory
            if len(picked) > 1:
                into = directory / _safe(dump_name or stem(picked[0][1]))
                into.mkdir(exist_ok=True)
                os.chmod(into, 0o777)
            share = bool(settings.get("reflinks", True))
            written = [_copy(folder / path, into, directory, share) for _, path in picked]
            _let_go(qb, info_hash, mine, settings.get("afterwards", "keep") == "remove")
            return written
        except BaseException:
            # A torrent added for this request goes again with what it downloaded, unless another request is using it;
            # one that was already there stays.
            try:
                _let_go(qb, info_hash, mine, added)
            except Exception:
                pass
            raise


def _let_go(qb: QBittorrent, info_hash: str, mine: str, remove: bool) -> None:
    """Takes this request's mark off the torrent, and removes it if asked and no other request is using it. The mark is
    deleted from qBittorrent's list of tags too, which otherwise keeps it after no torrent has it."""
    qb.remove_tags(info_hash, mine)
    qb.delete_tags([mine])
    if remove and (info := qb.info(info_hash)) is not None:
        tags = {tag.strip() for tag in str(info.get("tags") or "").split(",")}
        # Only a torrent Romdex added goes; one someone added by hand that a request used stays.
        if "romdex" in tags and not any(tag.startswith("romdex-") for tag in tags):
            qb.delete(info_hash, files=True)


def _job_tags(text: Any) -> set[str]:
    return {tag.strip() for tag in str(text or "").split(",") if tag.strip().startswith("romdex-")}


# The qBittorrents tidied since the plugin started, by address.
_tidied: set[str] = set()


def tidy(settings: dict[str, Any], running: set[str]) -> None:
    """Once after the plugin starts, for each qBittorrent: takes the marks of fetches that aren't running any more off
    their torrents (left by a fetch a restart cut short) and deletes every mark no torrent uses. The plain romdex tag
    stays: it says which torrents Romdex added."""
    url = str(settings.get("qbittorrentUrl") or "")
    if url in _tidied:
        return
    with QBittorrent(settings) as qb:
        wanted = {f"romdex-{job}" for job in running}
        used: set[str] = set()
        for found in qb.torrents():
            tags = _job_tags(found.get("tags"))
            stale = tags - wanted
            if stale:
                qb.remove_tags(found["hash"], ",".join(sorted(stale)))
            used |= tags & wanted
        qb.delete_tags(sorted(tag for tag in qb.tags() if tag.startswith("romdex-") and tag not in used))
    _tidied.add(url)


def _wait_for_files(job: Job, qb: QBittorrent, info_hash: str) -> list[dict[str, Any]]:
    """The torrent's files, once qBittorrent has them; a magnet link has to fetch them from peers first."""
    # qBittorrent adds a torrent in the background after saying it has, so for a moment it may not list it.
    seen, give_up = False, time.monotonic() + ADD_WAIT_SECONDS
    while True:
        job.check()
        if listed := qb.files(info_hash):
            return listed
        info = qb.info(info_hash)
        if info is None and not seen:
            if time.monotonic() > give_up:
                raise RuntimeError("qBittorrent didn't add the torrent.")
        else:
            seen = True
            _check(qb, info)
        time.sleep(POLL_SECONDS)


def _pick(chosen: dict[str, Any], listed: list[dict[str, Any]], target: Target | None, min_score: float) -> tuple[str | None, list[tuple[int, str]]]:
    """The dump's name and its files' indexes and paths, as qBittorrent has them."""
    if chosen.get("f"):
        names = {f["index"]: f["name"] for f in listed}
        # qBittorrent may have left out the torrent's top folder; its names are what's on disk.
        return chosen.get("d"), [(i, names.get(i, path)) for i, path in chosen["f"]]
    if target is None:
        raise RuntimeError("Romdex didn't say which game it wants from the torrent.")
    files = [torrents.TorrentFile(f["index"], f["name"], f["size"]) for f in listed]
    if (matched := match(target, files, min_score)) is None:
        raise RuntimeError(f"The torrent doesn't have a dump of {target.title}.")
    dump, picked, _ = matched
    return dump.name, [(f.index, f.path) for f in picked]


def _wait_for_download(job: Job, qb: QBittorrent, info_hash: str, wanted: set[int]) -> None:
    while True:
        job.check()
        info = qb.info(info_hash)
        _check(qb, info)
        files = [f for f in qb.files(info_hash) if f["index"] in wanted]
        # Another request sharing the torrent may have turned this one's files off as it added the torrent.
        if skipped := [f["index"] for f in files if f["priority"] == 0]:
            qb.priority(info_hash, skipped, 1)
            qb.start(info_hash)
            files = [f | {"priority": 1} for f in files]
        size = sum(f["size"] for f in files)
        done = sum(f["size"] * f["progress"] for f in files)
        if files and all(f["progress"] >= 1 for f in files):
            return
        job.progress = min(0.99, done / size) if size else None
        time.sleep(POLL_SECONDS)


def _check(qb: QBittorrent, info: dict[str, Any] | None) -> None:
    if info is None:
        raise RuntimeError("The torrent was removed from qBittorrent.")
    if info.get("state") in ("error", "missingFiles"):
        why = qb.problem(str(info.get("name") or ""))
        if why and ("Permission denied" in why or "No such file or directory" in why):
            # Most often the save folder is somewhere qBittorrent's container doesn't have.
            default = qb.default_save_path()
            why += (". Check that the Prowlarr plugin's Save folder is a folder qBittorrent has, as qBittorrent sees it"
                    + (f" (its default is {default})" if default else "") + ".")
        raise RuntimeError(f"qBittorrent stopped the torrent ({info['state']}): {why}" if why else
                           f"qBittorrent stopped the torrent ({info['state']}); its log says why, often that it can't "
                           f"write to the save folder.")


def save_folder_hint(save_path: str, default: str | None) -> str | None:
    """Says so when the save folder isn't under qBittorrent's own default, so may be somewhere qBittorrent doesn't have
    (a different mount in its container). Only a warning: qBittorrent may have that folder mounted too."""
    if not default:
        return None
    save, base = PurePosixPath(save_path.rstrip("/") or "/"), PurePosixPath(default.rstrip("/") or "/")
    if save == base or base in save.parents:
        return None
    return (f"qBittorrent saves under {base}, but the save folder is {save}, which qBittorrent may not have; "
            f"try {base / 'romdex'}")


def _local_folder(settings: dict[str, Any], info: dict[str, Any]) -> Path:
    """Where this plugin sees the torrent's folder: qBittorrent's save path, with the start swapped for the plugin's."""
    save_path = str(settings.get("savePath") or "/downloads/romdex").rstrip("/")
    local = str(settings.get("downloadsFolder") or save_path).rstrip("/")
    actual = str(info.get("save_path") or save_path).rstrip("/")
    if actual == save_path or actual.startswith(save_path + "/"):
        actual = local + actual[len(save_path):]
    return Path(actual)


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)


def _copy(source: Path, into: Path, directory: Path, share: bool) -> dict[str, Any]:
    # Copied rather than moved, so qBittorrent can keep seeding it; as a reflink where it can be, so the copy takes
    # next to no space.
    if ".." in source.parts or not source.is_file():
        raise RuntimeError(f"Can't see {source}; check that this plugin can read qBittorrent's downloads folder.")
    target = into / _safe(source.name)
    if share:
        reflinks.clone(source, target)
    else:
        shutil.copyfile(source, target)
    # The core and the igir worker run as other users, and need to read and move it.
    os.chmod(target, 0o666)
    return {"path": target.relative_to(directory).as_posix(), "size": target.stat().st_size}


def check_reflinks(settings: dict[str, Any], staging: Path) -> tuple[bool, str]:
    """Whether a file qBittorrent downloaded can be reflinked into staging, tried with one already there."""
    folder = _local_folder(settings, {})
    if not folder.is_dir():
        return False, f"This plugin can't see qBittorrent's downloads folder at {folder}; mount it into its container."
    sample = next((f for f in _files_under(folder) if f.stat().st_size > 0), None)
    if sample is None:
        return False, ("There's nothing in qBittorrent's downloads folder to try it with yet; check again once "
                       "something has downloaded.")
    target = staging / f".romdex-reflink-check-{os.getpid()}"
    try:
        works, why = reflinks.try_clone(sample, target)
    finally:
        target.unlink(missing_ok=True)
    if works:
        return True, "From qBittorrent's downloads to staging, files can share their disk space."
    return False, f"From qBittorrent's downloads to staging, files can't share their disk space: {why}."


def _files_under(folder: Path, limit: int = 2000):
    """Files under the folder, a bounded number of them."""
    seen = 0
    for root, _, files in os.walk(folder):
        for name in files:
            yield Path(root) / name
            seen += 1
            if seen >= limit:
                return
