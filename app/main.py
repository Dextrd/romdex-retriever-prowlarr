"""The Prowlarr and qBittorrent retrieval plugin's HTTP API (Romdex retrieval contract v1.0)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse

from . import retrieve
from .clients import Prowlarr, QBittorrent
from .contract import CONTRACT_VERSION, FetchRequest, HealthRequest, Jobs, SearchRequest, dump, env, error, staging_directory

VERSION = "1.0.0"
STAGING = env("STAGING_ROOT", "/staging")

SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prowlarrUrl": {"type": "string", "default": "http://prowlarr:9696", "title": "Prowlarr address"},
        "prowlarrApiKey": {"type": "string", "title": "Prowlarr API key", "writeOnly": True,
                           "description": "In Prowlarr under Settings → General."},
        "qbittorrentUrl": {"type": "string", "default": "http://qbittorrent:8080", "title": "qBittorrent address",
                           "description": "The address of qBittorrent's Web UI."},
        "qbittorrentUsername": {"type": "string", "default": "", "title": "qBittorrent username",
                                "description": "Leave empty when qBittorrent lets this plugin in without logging in."},
        "qbittorrentPassword": {"type": "string", "default": "", "title": "qBittorrent password", "writeOnly": True},
        "category": {"type": "string", "default": "romdex", "title": "Category",
                     "description": "The qBittorrent category torrents are added with."},
        "savePath": {"type": "string", "default": "/downloads/romdex", "title": "Save folder",
                     "description": "Where qBittorrent saves torrents added for Romdex, as qBittorrent sees it: a "
                                    "folder in qBittorrent's container. If qBittorrent has your downloads disk at "
                                    "/data, say, this is /data/torrents/romdex, even though this plugin sees it "
                                    "at /downloads/romdex."},
        "downloadsFolder": {"type": "string", "default": "", "title": "Save folder for this plugin",
                            "description": "The same folder as this plugin's container sees it, when it's mounted "
                                           "somewhere else, such as /downloads/romdex with DOWNLOADS_PATH mounted at "
                                           "/downloads. Leave empty when it's the same path."},
        "minSeeders": {"type": "integer", "minimum": 0, "default": 1, "title": "Fewest seeders",
                       "description": "Torrents with fewer seeders aren't offered."},
        "minScore": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.6, "title": "Lowest match score",
                     "description": "Torrents that match the game less well than this aren't offered. A file named "
                                    "as the DAT names a dump always is."},
        "afterwards": {"type": "string", "enum": retrieve.AFTER, "default": "keep", "title": "Once copied",
                       "description": "keep leaves the torrent in qBittorrent to seed. remove takes it out, with "
                                      "its files, once Romdex has a copy."},
        "reflinks": {"type": "boolean", "default": True, "title": "Share disk space with the seeding files",
                     "description": "Copies files as reflinks, which share their data on disk with the files "
                                    "qBittorrent seeds, so games you keep seeding aren't stored twice. Where the "
                                    "filesystem can't make them, files are copied as usual; Check reflinks says "
                                    "which."},
    },
}

MANIFEST: dict[str, Any] = {
    "id": "romdex.retrieval.prowlarr",
    "name": "Prowlarr",
    "version": VERSION,
    "type": "retrieval",
    "contractVersion": CONTRACT_VERSION,
    "description": "Searches the torrent indexers you've set up in Prowlarr and downloads with your qBittorrent, "
                   "taking only the files of the dump asked for.",
    "homepage": "https://github.com/Dextrd/romdex-retriever-prowlarr",
    "capabilities": ["search", "cancel"],
    "settingsSchema": SETTINGS_SCHEMA,
    "access": {"network": ["your Prowlarr", "your qBittorrent"], "files": ["staging (write)", "qBittorrent's downloads (read)"]},
    "checks": [{"id": "reflinks", "title": "Check reflinks",
                "description": "Whether files can share their disk space from qBittorrent's downloads to staging."}],
}

app = FastAPI(title="Romdex Prowlarr retriever", version=VERSION)
jobs = Jobs(int(env("PARALLEL_FETCHES", "2")))


@app.exception_handler(HTTPException)
async def contract_error(_: Request, e: HTTPException) -> JSONResponse:
    body = e.detail if isinstance(e.detail, dict) else {"error": str(e.detail)}
    return JSONResponse(body, status_code=e.status_code)


# Not marked required in the schema: Romdex adds a plugin before its settings can be filled in, and health says
# what's missing until they are.
NEEDED = ["prowlarrUrl", "prowlarrApiKey", "qbittorrentUrl"]


def check_settings(settings: dict[str, Any]) -> None:
    for name in NEEDED:
        if not settings.get(name):
            raise error(400, f"{SETTINGS_SCHEMA['properties'][name]['title']} is needed.", "invalid-settings")
    if settings.get("afterwards", "keep") not in retrieve.AFTER:
        raise error(400, f"afterwards must be one of {', '.join(retrieve.AFTER)}.", "invalid-settings")


@app.get("/v1/manifest")
def manifest() -> dict[str, Any]:
    return MANIFEST


@app.post("/v1/checks/{check}")
def run_check(check: str, body: HealthRequest) -> dict[str, Any]:
    if check != "reflinks":
        raise error(404, f"This plugin has no check called {check}.", "unknown-check")
    passed, message = retrieve.check_reflinks(body.settings, Path(STAGING))
    return {"passed": passed, "message": message}


@app.post("/v1/health")
def health(body: HealthRequest) -> dict[str, Any]:
    settings = body.settings
    try:
        check_settings(settings)
    except HTTPException as e:
        return {"status": "unhealthy", "message": e.detail["error"]}
    try:
        with Prowlarr(settings) as prowlarr:
            version, indexers = prowlarr.status().get("version"), len(prowlarr.indexers())
    except Exception as e:
        return {"status": "unhealthy", "message": f"Couldn't reach Prowlarr: {e}"}
    try:
        with QBittorrent(settings) as qb:
            qb_version = qb.version()
            default_save = qb.default_save_path()
    except Exception as e:
        return {"status": "unhealthy", "message": f"Couldn't reach qBittorrent: {e}"}
    tidy(settings)
    message = f"Prowlarr {version} with {indexers} indexer{'s' if indexers != 1 else ''}, qBittorrent {qb_version}"
    # Both sides of the save folder: qBittorrent's, then this plugin's.
    problems = []
    if hint := retrieve.save_folder_hint(str(settings.get("savePath") or "/downloads/romdex"), default_save):
        problems.append(hint)
    folder = Path(str(settings.get("downloadsFolder") or settings.get("savePath") or "/downloads/romdex"))
    if not folder.parent.is_dir():
        problems.append(f"this plugin can't see {folder.parent}; mount qBittorrent's downloads folder there")
    if problems:
        return {"status": "degraded", "message": f"{message}, but {'; and '.join(problems)}."}
    if indexers == 0:
        return {"status": "degraded", "message": f"{message}; add indexers in Prowlarr to find anything."}
    return {"status": "healthy", "message": message}


@app.post("/v1/search")
def search(body: SearchRequest) -> dict[str, Any]:
    check_settings(body.settings)
    try:
        candidates = retrieve.search(body.target, max(1, min(body.limit, 25)), body.settings)
    except PermissionError as e:
        raise error(502, str(e), "source-unavailable")
    except Exception as e:
        raise error(502, f"Prowlarr search failed: {e}", "source-unavailable")
    return {"candidates": dump(candidates)}


def tidy(settings: dict[str, Any], starting: str | None = None) -> None:
    """Clears up marks left on qBittorrent's torrents, once after starting; never in the way of what's asked."""
    try:
        retrieve.tidy(settings, jobs.running() | ({starting} if starting else set()))
    except Exception:
        pass


@app.post("/v1/fetch", status_code=202)
def fetch(body: FetchRequest) -> dict[str, Any]:
    check_settings(body.settings)
    directory = staging_directory(STAGING, body.staging_directory)
    tidy(body.settings, body.job_id)
    job = jobs.start(body.job_id, lambda j: retrieve.fetch(j, body.candidate.candidate_id, body.target, directory, body.settings))
    return job.status()


@app.get("/v1/fetch/{job_id}")
def status(job_id: str) -> dict[str, Any]:
    if (job := jobs.get(job_id)) is None:
        raise error(404, "No such job; the plugin may have restarted.", "unknown-job")
    return job.status()


@app.delete("/v1/fetch/{job_id}", status_code=204)
def cancel(job_id: str) -> None:
    if (job := jobs.get(job_id)) is None:
        raise error(404, "No such job; the plugin may have restarted.", "unknown-job")
    job.cancelled.set()
