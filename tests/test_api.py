import errno
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app import clients, main, retrieve, torrent


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(value[k]) for k in sorted(value)) + b"e"


def make_torrent(name: str, files: list[tuple[str, int]]) -> tuple[bytes, str]:
    info = {"name": name, "piece length": 16384, "pieces": b"\0" * 20,
            "files": [{"path": path.split("/"), "length": size} for path, size in files]}
    return bencode({"announce": "http://tracker.invalid/announce", "info": info}), hashlib.sha1(bencode(info)).hexdigest()


# Made-up games, as Romdex's fixtures are.
# One game on its own, a system's whole set, a disc and a game packed in a 7z.
POND = [("Pixel Pond (USA).gb", 32768), ("Pixel Pond (USA).txt", 200)]
POND_TORRENT, POND_HASH = make_torrent("Pixel Pond (USA)", POND)
SET = [("Frog Racer (Europe).gb", 65536), ("Pixel Pond (Japan).gb", 32768), ("Pixel Pond (USA).gb", 32768),
       ("Zap Lily (USA).gb", 16384)]
SET_TORRENT, SET_HASH = make_torrent("Made-up Handheld - Complete Set", SET)
PACKED_TORRENT, PACKED_HASH = make_torrent("Pixel Pond", [("Pixel Pond (USA).7z", 9000), ("cover.jpg", 1000)])
DISC = [("Lily Leap (Europe)/Lily Leap (Europe).cue", 300), ("Lily Leap (Europe)/Lily Leap (Europe) (Track 1).bin", 9_000_000),
        ("Lily Leap (Europe)/Lily Leap (Europe) (Track 2).bin", 1_000_000), ("Lily Leap (Europe)/scans/front.jpg", 5000)]
DISC_TORRENT, DISC_HASH = make_torrent("Lily Leap", DISC)

POND_GAME = {"system": "Made-up - Handheld", "title": "Pixel Pond", "dumps": [
    {"name": "Pixel Pond (USA)", "regions": ["USA"], "files": [{"name": "Pixel Pond (USA).gb", "size": 32768, "crc32": "1234abcd"}]},
    {"name": "Pixel Pond (Japan)", "regions": ["Japan"], "files": [{"name": "Pixel Pond (Japan).gb", "size": 32768}]},
]}
LEAP_GAME = {"system": "Made-up - Disc", "title": "Lily Leap", "dumps": [
    {"name": "Lily Leap (Europe)", "regions": ["Europe"], "files": [
        {"name": "Lily Leap (Europe).cue", "size": 300}, {"name": "Lily Leap (Europe) (Track 1).bin", "size": 9_000_000},
        {"name": "Lily Leap (Europe) (Track 2).bin", "size": 1_000_000}]},
]}
SETTINGS = {"prowlarrUrl": "http://prowlarr:9696", "prowlarrApiKey": "test-key", "qbittorrentUrl": "http://qbittorrent:8080"}


def release(title, link=None, magnet=None, info_hash=None, seeders=5, protocol="torrent"):
    return {"title": title, "indexer": "Test tracker", "seeders": seeders, "protocol": protocol, "infoHash": info_hash,
            "downloadUrl": link, "magnetUrl": magnet, "infoUrl": f"https://tracker.invalid/{title}"}


class FakeProwlarr:
    def __init__(self, results, torrents):
        self.results, self.torrents, self.queries, self.keys = results, torrents, [], []
        self.answers = lambda query: True  # whether the indexers have anything for a search
        self.failing: set[str] = set()  # searches Prowlarr fails

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.keys.append(request.headers.get("X-Api-Key") or request.url.params.get("apikey"))
        if request.url.path == "/api/v1/search":
            q = request.url.params["query"]
            self.queries.append(q)
            if not self.answers(q):
                return httpx.Response(200, json=[])
            if q in self.failing:
                return httpx.Response(500)
            # Results for every search, or for each its own.
            return httpx.Response(200, json=self.results.get(q, []) if isinstance(self.results, dict) else self.results)
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(200, json={"version": "2.6.5"})
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(200, json=[{"name": "Test tracker", "enable": True}])
        if request.url.path.startswith("/1/download"):
            got = self.torrents.get(request.url.params["file"])
            if isinstance(got, str):
                return httpx.Response(301, headers={"location": got})
            return httpx.Response(200, content=got) if got else httpx.Response(404)
        return httpx.Response(404)


class FakeQBittorrent:
    """qBittorrent with torrents that finish downloading as soon as they're started."""

    def __init__(self, folder: Path, torrents: dict[str, bytes]):
        self.folder, self.known, self.added, self.deleted, self.priorities = folder, torrents, {}, [], {}
        self.magnets: dict[str, bytes] = {}  # a magnet link, and the torrent its peers would send
        self.forms: list[dict[str, list[str]]] = []
        self.default_save = "/downloads"
        self.unlisted = 0  # how many times a torrent just added isn't listed yet, as qBittorrent adds it in the background
        self.tag_list: set[str] = set()  # qBittorrent keeps every tag ever used until it's deleted

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v2")
        form = parse_qs(request.content.decode(errors="replace")) if request.method == "POST" and b"Content-Disposition" not in request.content else {}
        if path == "/app/version":
            return httpx.Response(200, text="v5.1.4")
        if path == "/app/defaultSavePath":
            return httpx.Response(200, text=self.default_save)
        if path == "/torrents/add":
            body = request.content
            self.forms.append(form)
            if "urls" in form:
                data = self.magnets[form["urls"][0]]
            else:
                data = next(t for t in self.known.values() if t in body)
            info_hash, files = torrent.read(data)
            # Like qBittorrent, a stopped magnet link never fetches its file list.
            metadata = "urls" not in form or (form["stopped"] == ["false"] and form.get("stopCondition") == ["MetadataReceived"])
            tags = form["tags"][0] if "tags" in form else re.search(rb'name="tags"\r\n\r\n(.*?)\r\n', body)[1].decode()
            self.added[info_hash] = {"files": files, "started": False, "body": body, "tags": set(tags.split(",")),
                                     "metadata": metadata, "unlisted": self.unlisted}
            self.tag_list |= set(tags.split(","))
            return httpx.Response(200, text="Ok.")
        if path in ("/torrents/addTags", "/torrents/removeTags"):
            t = self.added[form["hashes"][0]]
            named = set(form["tags"][0].split(","))
            t["tags"] = t["tags"] | named if path.endswith("addTags") else t["tags"] - named
            if path.endswith("addTags"):
                self.tag_list |= named
            return httpx.Response(200)
        if path == "/torrents/deleteTags":
            named = set(form["tags"][0].split(","))
            self.tag_list -= named
            for t in self.added.values():
                t["tags"] -= named
            return httpx.Response(200)
        if path == "/torrents/tags":
            return httpx.Response(200, json=sorted(self.tag_list - {""}))
        if path == "/torrents/info" and "hashes" not in request.url.params:
            return httpx.Response(200, json=[{"hash": h, "name": "Pond", "tags": ", ".join(sorted(t["tags"] - {""}))} for h, t in self.added.items()])
        if path == "/torrents/info":
            h = request.url.params["hashes"]
            t = self.added.get(h)
            if t and t.get("unlisted"):
                t["unlisted"] -= 1
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"hash": h, "name": "Pond", "state": t.get("state") or ("uploading" if t["started"] else "stoppedDL"),
                                              "save_path": "/downloads/romdex", "tags": ", ".join(sorted(t["tags"] - {""}))}] if t else [])
        if path == "/log/main":
            return httpx.Response(200, json=[{"id": 1, "message": 'File error alert. Torrent: "Pond". Reason: "Permission denied"',
                                              "timestamp": 0, "type": 4}])
        if path == "/torrents/files":
            t = self.added.get(request.url.params["hash"])
            if not t or t.get("unlisted"):
                return httpx.Response(404)
            if not t["metadata"]:
                return httpx.Response(200, json=[])
            prio = self.priorities.get(request.url.params["hash"], {})
            return httpx.Response(200, json=[{"index": f.index, "name": f.path, "size": f.size, "priority": prio.get(f.index, 1),
                                              "progress": 1 if t["started"] and prio.get(f.index, 1) else 0} for f in t["files"]])
        if path == "/torrents/filePrio":
            h = form["hash"][0]
            for i in form["id"][0].split("|"):
                self.priorities.setdefault(h, {})[int(i)] = int(form["priority"][0])
            return httpx.Response(200)
        if path == "/torrents/start":
            t = self.added[form["hashes"][0]]
            t["started"] = True
            for f in t["files"]:
                if self.priorities.get(form["hashes"][0], {}).get(f.index, 1):
                    (self.folder / f.path).parent.mkdir(parents=True, exist_ok=True)
                    (self.folder / f.path).write_bytes(b"data " + f.path.encode())
            return httpx.Response(200)
        if path == "/torrents/delete":
            self.deleted.append((form["hashes"][0], form["deleteFiles"][0]))
            self.added.pop(form["hashes"][0], None)
            return httpx.Response(200)
        return httpx.Response(404)


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    (tmp_path / "staging").mkdir()
    monkeypatch.setattr(main, "STAGING", str(tmp_path / "staging"))
    monkeypatch.setattr(retrieve, "POLL_SECONDS", 0.01)
    torrents = {"pond": POND_TORRENT, "set": SET_TORRENT, "packed": PACKED_TORRENT, "disc": DISC_TORRENT}
    prowlarr = FakeProwlarr([
        release("Pixel Pond (USA)", "http://prowlarr:9696/1/download?apikey=test-key&file=pond", seeders=12),
        release("Made-up Handheld - Complete Set", "http://prowlarr:9696/1/download?apikey=test-key&file=set", seeders=40),
        release("Pixel Pond (USA) [usenet]", "http://prowlarr:9696/1/download?file=nzb", protocol="usenet"),
        release("Pixel Pond (USA) [dead]", "http://prowlarr:9696/1/download?file=pond", seeders=0),
    ], torrents)
    qb = FakeQBittorrent(tmp_path / "downloads", torrents)

    def fake_http(base_url, headers):
        handler = qb if "qbittorrent" in base_url else prowlarr
        return httpx.Client(base_url=base_url, headers=headers, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(clients, "http", fake_http)
    # Each test's qBittorrent is new, so tidied afresh.
    monkeypatch.setattr(retrieve, "_tidied", set())
    settings = {**SETTINGS, "downloadsFolder": str(tmp_path / "downloads")}
    return TestClient(main.app), prowlarr, qb, settings


def search(client, settings, target):
    return client.post("/v1/search", json={"settings": settings, "target": target}).json()["candidates"]


def fetch(client, settings, target, candidate, job):
    started = client.post("/v1/fetch", json={"settings": settings, "jobId": job, "target": target, "candidate": candidate,
                                             "stagingDirectory": job})
    assert started.status_code == 202
    return wait(client, job)

@pytest.mark.parametrize("answer, error", [
    (httpx.Response(200, text="Ok."), None),
    (httpx.Response(204), None),
    (httpx.Response(200, text="Fails."), "refused the username and password"),
    (httpx.Response(401), "refused the username and password"),
    (httpx.Response(403), "too many failed sign-ins"),
])
def test_signing_in_to_qbittorrent_understands_old_and_new_answers(monkeypatch, answer, error):
    # qBittorrent 5.2 answers a sign-in with 204 and nothing else, where earlier versions said "Ok.".
    def handler(request):
        return answer if request.url.path.endswith("/auth/login") else httpx.Response(200, text="v5.2.0")
    monkeypatch.setattr(clients, "http", lambda base_url, headers: httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler)))
    settings = {"qbittorrentUrl": "http://qbittorrent:8080", "qbittorrentUsername": "admin", "qbittorrentPassword": "secret"}
    if error is None:
        assert clients.QBittorrent(settings).version() == "v5.2.0"
    else:
        with pytest.raises(PermissionError, match=error):
            clients.QBittorrent(settings)


def wait(client, job_id):
    for _ in range(300):
        status = client.get(f"/v1/fetch/{job_id}").json()
        if status["state"] not in ("queued", "running"):
            return status
        time.sleep(0.01)
    raise TimeoutError(status)


def test_manifest_declares_the_contract():
    manifest = TestClient(main.app).get("/v1/manifest").json()
    assert manifest["type"] == "retrieval"
    assert manifest["contractVersion"] == "1.0"
    secrets = [k for k, v in manifest["settingsSchema"]["properties"].items() if v.get("writeOnly")]
    assert secrets == ["prowlarrApiKey", "qbittorrentPassword"]



def test_settings_without_prowlarr_are_refused():
    response = TestClient(main.app).post("/v1/search", json={"settings": {}, "target": POND_GAME})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid-settings"


def test_a_game_is_offered_from_torrents_with_a_file_named_as_its_dat_names_a_dump(fakes):
    client, prowlarr, _, settings = fakes
    candidates = search(client, settings, POND_GAME)
    # Usenet and unseeded releases aren't torrents to fetch; between equally good ones, the better-seeded first.
    assert [(c["dumpName"], c["source"], c["score"]) for c in candidates] == [
        ("Pixel Pond (USA)", "Test tracker (40 seeders)", 1.0), ("Pixel Pond (USA)", "Test tracker (12 seeders)", 1.0)]
    chosen = json.loads(candidates[0]["candidateId"])
    assert chosen["h"] == SET_HASH and chosen["d"] == "Pixel Pond (USA)"
    assert chosen["f"] == [[2, "Made-up Handheld - Complete Set/Pixel Pond (USA).gb"]]  # just its file of the set
    assert "apikey" not in chosen["t"]
    assert candidates[0]["format"] == "gb" and candidates[0]["size"] == 32768
    assert "just these files" in candidates[0]["notes"]
    # The game, and its system for torrents of a whole set; a system Romdex doesn't know has no short name to add.
    assert sorted(prowlarr.queries) == ["Handheld", "Pixel Pond"]


def test_the_dump_asked_for_is_the_one_offered(fakes):
    client, _, _, settings = fakes
    candidates = search(client, settings, {**POND_GAME, "dumpName": "Pixel Pond (Japan)"})
    # Only the set has the Japanese dump.
    assert [c["dumpName"] for c in candidates] == ["Pixel Pond (Japan)"]
    assert json.loads(candidates[0]["candidateId"])["f"] == [[1, "Made-up Handheld - Complete Set/Pixel Pond (Japan).gb"]]


def test_a_dump_packed_in_an_archive_named_after_it_is_offered(fakes):
    client, prowlarr, _, settings = fakes
    prowlarr.results = [release("Pixel Pond", "http://prowlarr:9696/1/download?file=packed")]
    [candidate] = search(client, settings, POND_GAME)
    assert (candidate["dumpName"], candidate["format"], candidate["score"]) == ("Pixel Pond (USA)", "7z", 0.95)


def test_a_file_named_otherwise_is_offered_by_how_alike_its_name_is(fakes):
    client, prowlarr, _, settings = fakes
    prowlarr.torrents["odd"], _ = make_torrent("Pixel Pond", [("Pixel Pond USA.gb", 32768), ("Readme.txt", 10)])
    prowlarr.torrents["other"], _ = make_torrent("Other", [("Something Else (USA).gb", 32768)])
    prowlarr.results = [release("Pixel Pond", "http://prowlarr:9696/1/download?file=odd"),
                        release("Pixel Pond Other", "http://prowlarr:9696/1/download?file=other")]
    [candidate] = search(client, settings, POND_GAME)
    assert json.loads(candidate["candidateId"])["f"] == [[0, "Pixel Pond/Pixel Pond USA.gb"]]
    assert 0.6 <= candidate["score"] < 0.95


def test_a_magnet_only_release_is_offered_lower_on_its_name(fakes):
    client, prowlarr, _, settings = fakes
    magnet = "magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=Pixel+Pond"
    prowlarr.results = [release("Pixel Pond (USA)", "http://prowlarr:9696/1/download?file=magnet", seeders=8)]
    prowlarr.torrents["magnet"] = magnet
    [candidate] = search(client, settings, POND_GAME)
    assert json.loads(candidate["candidateId"]) == {"h": "ab" * 20, "m": magnet, "d": None, "f": None}
    assert 0.6 <= candidate["score"] < 0.9


# The Legend of Zelda on the NES, which shares its name with games on every Nintendo system since.
ZELDA_GAME = {"system": "Nintendo - Nintendo Entertainment System", "title": "The Legend of Zelda", "dumps": [
    {"name": "Legend of Zelda, The (Europe) (Rev 1)", "regions": ["Europe"],
     "files": [{"name": "Legend of Zelda, The (Europe) (Rev 1).nes", "size": 131088}]},
]}


def link(file):
    return f"http://prowlarr:9696/1/download?file={file}"


def test_a_game_is_found_on_its_own_system_among_better_seeded_games_of_the_same_name(fakes):
    client, prowlarr, _, settings = fakes
    zelda = [("Legend of Zelda, The (Europe) (Rev 1).nes", 131088)]
    nes_set = [("Nintendo - Nintendo Entertainment System (Headered)/Legend of Zelda, The (Europe) (Rev 1).nes", 131088),
               ("Nintendo - Nintendo Entertainment System (Headered)/Zap Lily (USA).nes", 40976)]
    prowlarr.torrents["nes"], _ = make_torrent("The Legend of Zelda (NES)", zelda)
    prowlarr.torrents["set"], _ = make_torrent("No-Intro NES", nes_set)
    # Ten games of the same name on later systems, all better seeded; one as a file named just as the NES game is.
    later = [release(f"The Legend of Zelda {name} [NSP]", link(f"later{i}"), seeders=900 - i)
             for i, name in enumerate(["Breath of the Wild", "Tears of the Kingdom", "Links Awakening",
                                       "Skyward Sword HD", "Echoes of Wisdom"])]
    later += [release(f"The Legend of Zelda {name} (Wii U)", link(f"later{i + 5}"), seeders=500 - i)
              for i, name in enumerate(["Breath of the Wild", "Twilight Princess HD", "Wind Waker HD"])]
    for i in range(8):
        prowlarr.torrents[f"later{i}"], _ = make_torrent("Zelda", [("The Legend of Zelda.nsp", 10 ** 9)])
    magnet = "magnet:?xt=urn:btih:" + "cd" * 20
    prowlarr.results = {
        "The Legend of Zelda": [*later, release("The Legend of Zelda Breath of the Wild", magnet=magnet, seeders=2000)],
        "The Legend of Zelda NES": [release("The Legend of Zelda (NES)", link("nes"), seeders=3)],
        "Nintendo Entertainment System": [release("Nintendo - Nintendo Entertainment System (Headered) (No-Intro 2025)",
                                                  link("set"), seeders=20)],
    }
    candidates = search(client, settings, ZELDA_GAME)
    assert sorted(prowlarr.queries) == ["Nintendo Entertainment System", "The Legend of Zelda", "The Legend of Zelda NES"]
    # Just the NES game, from the torrent of the game and from the set; none of the later games, magnet link or not.
    assert [(c["dumpName"], c["source"], c["score"]) for c in candidates] == [
        ("Legend of Zelda, The (Europe) (Rev 1)", "Test tracker (20 seeders)", 1.0),
        ("Legend of Zelda, The (Europe) (Rev 1)", "Test tracker (3 seeders)", 1.0)]
    assert json.loads(candidates[0]["candidateId"])["f"] == [[0, f"No-Intro NES/{nes_set[0][0]}"]]


def test_a_release_named_for_another_system_ranks_below_the_games_own():
    target = retrieve.Target.model_validate(ZELDA_GAME)
    score = lambda name: retrieve.title_score(target, name)
    assert score("The Legend of Zelda (NES)") == 1.0
    assert score("The Legend of Zelda (Famicom) [Europe]") == 1.0
    # Unsaid, it may be the game, but less surely.
    assert score("The Legend of Zelda") == pytest.approx(retrieve.NO_SYSTEM)
    # Another game of the series has words of its own; on another system, it's further down still.
    assert score("The Legend of Zelda Breath of the Wild") < 0.65
    assert score("The Legend of Zelda Breath of the Wild [NSP]") < 0.35
    assert score("The Legend of Zelda (Switch)") == pytest.approx(retrieve.OTHER_SYSTEM)
    # The SNES's long name isn't read as the NES's it ends with.
    snes = retrieve.Target.model_validate({**ZELDA_GAME, "system": "Nintendo - Super Nintendo Entertainment System",
                                           "title": "A Link to the Past"})
    assert retrieve.system_fit(snes, "Zelda - A Link to the Past (Super Nintendo Entertainment System)")[0] == retrieve.ON_SYSTEM
    assert retrieve.system_fit(target, "Zelda - A Link to the Past (Super Nintendo Entertainment System)")[0] == retrieve.OTHER_SYSTEM
    # A system's name in the game's own isn't the system it's for.
    wars = retrieve.Target.model_validate({**ZELDA_GAME, "title": "Famicom Wars", "dumps": []})
    assert retrieve.system_fit(wars, "Famicom Wars")[0] == retrieve.NO_SYSTEM


def test_another_systems_file_isnt_offered_for_a_name_like_the_games(fakes):
    client, prowlarr, _, settings = fakes
    prowlarr.torrents["switch"], _ = make_torrent("Zelda", [("Legend of Zelda, The (Europe).nsp", 10 ** 9)])
    prowlarr.torrents["nes"], _ = make_torrent("Zelda", [("Legend of Zelda, The (Europe).nes", 131088)])
    prowlarr.results = [release("The Legend of Zelda [EU]", link("switch"), seeders=50),
                        release("The Legend of Zelda (Europe)", link("nes"), seeders=5)]
    [candidate] = search(client, settings, ZELDA_GAME)
    assert json.loads(candidate["candidateId"])["f"] == [[0, "Zelda/Legend of Zelda, The (Europe).nes"]]


def test_the_games_search_failing_fails_but_the_others_only_add_to_it(fakes):
    client, prowlarr, _, settings = fakes
    prowlarr.torrents["nes"], _ = make_torrent("Zelda", [("Legend of Zelda, The (Europe) (Rev 1).nes", 131088)])
    prowlarr.results = [release("The Legend of Zelda (NES)", link("nes"))]
    prowlarr.failing = {"The Legend of Zelda NES", "Nintendo Entertainment System"}
    assert len(search(client, settings, ZELDA_GAME)) == 1
    prowlarr.failing = {"The Legend of Zelda"}
    response = client.post("/v1/search", json={"settings": settings, "target": ZELDA_GAME})
    assert response.status_code == 502


def test_a_game_is_downloaded_with_just_its_file_and_left_seeding(fakes):
    client, prowlarr, qb, settings = fakes
    candidate = search(client, settings, POND_GAME)[0]
    status = fetch(client, settings, POND_GAME, candidate, "job-1")

    assert status["state"] == "completed", status
    assert status["files"] == [{"path": "Pixel Pond (USA).gb", "size": len(b"data Made-up Handheld - Complete Set/Pixel Pond (USA).gb")}]
    assert qb.priorities[SET_HASH] == {0: 0, 1: 0, 3: 0}  # the rest of the set isn't downloaded
    assert SET_HASH in qb.added and qb.deleted == []
    assert b"romdex-job-1" in qb.added[SET_HASH]["body"]
    assert "test-key" in prowlarr.keys[-1]  # the key is put back to fetch the .torrent


def test_a_disc_keeps_its_cue_sheet_and_tracks_together_in_a_folder(fakes, tmp_path):
    client, prowlarr, qb, settings = fakes
    prowlarr.results = [release("Lily Leap", "http://prowlarr:9696/1/download?file=disc")]
    [candidate] = search(client, settings, LEAP_GAME)
    assert candidate["score"] == 1.0 and candidate["format"] == "bin,cue"
    status = fetch(client, settings, LEAP_GAME, candidate, "job-disc")

    assert status["state"] == "completed", status
    assert [f["path"] for f in status["files"]] == [
        "Lily Leap (Europe)/Lily Leap (Europe).cue", "Lily Leap (Europe)/Lily Leap (Europe) (Track 1).bin",
        "Lily Leap (Europe)/Lily Leap (Europe) (Track 2).bin"]
    assert qb.priorities[DISC_HASH] == {3: 0}  # not the scans
    staged = tmp_path / "staging" / "job-disc" / "Lily Leap (Europe)" / "Lily Leap (Europe).cue"
    assert staged.stat().st_mode & 0o666 == 0o666


def test_a_finished_fetch_leaves_no_tag_of_its_own_behind(fakes):
    client, _, qb, settings = fakes
    candidate = search(client, settings, POND_GAME)[0]
    assert fetch(client, settings, POND_GAME, candidate, "job-tags")["state"] == "completed"

    assert qb.added[SET_HASH]["tags"] == {"romdex"}
    assert qb.tag_list == {"romdex"}


def test_tags_left_by_old_fetches_are_cleared_once_when_qbittorrent_is_first_reached(fakes):
    client, _, qb, settings = fakes
    info_hash, files = torrent.read(POND_TORRENT)
    # Left by fetches a restart cut short, and one only in qBittorrent's list; a torrent added by hand keeps its own tags.
    qb.added[info_hash] = {"files": files, "started": True, "body": b"", "tags": {"romdex", "romdex-old-1", "romdex-old-2"}, "metadata": True}
    qb.added["handmade"] = {"files": [], "started": True, "body": b"", "tags": {"games"}, "metadata": True}
    qb.tag_list = {"romdex", "romdex-old-1", "romdex-old-2", "romdex-gone", "games"}

    assert client.post("/v1/health", json={"settings": settings}).json()["status"] in ("healthy", "degraded")

    assert qb.added[info_hash]["tags"] == {"romdex"} and qb.added["handmade"]["tags"] == {"games"}
    assert qb.tag_list == {"romdex", "games"}
    qb.tag_list.add("romdex-later")
    client.post("/v1/health", json={"settings": settings})
    assert "romdex-later" in qb.tag_list  # only once


def test_a_torrent_can_be_removed_afterwards(fakes):
    client, _, qb, settings = fakes
    settings = {**settings, "afterwards": "remove"}
    candidate = search(client, settings, POND_GAME)[0]
    assert fetch(client, settings, POND_GAME, candidate, "job-2")["state"] == "completed"
    assert qb.deleted == [(SET_HASH, "true")]


def test_a_torrent_that_was_already_there_is_kept_when_the_fetch_fails(fakes, tmp_path):
    client, _, qb, settings = fakes
    info_hash, files = torrent.read(SET_TORRENT)
    qb.added[info_hash] = {"files": files, "started": True, "body": b"", "tags": set(), "metadata": True}
    settings = {**settings, "downloadsFolder": str(tmp_path / "elsewhere")}  # where the files aren't
    candidate = search(client, settings, POND_GAME)[0]
    status = fetch(client, settings, POND_GAME, candidate, "job-3")

    assert status["state"] == "failed"
    assert "downloads folder" in status["error"]
    assert qb.deleted == []


def test_a_magnet_link_fetches_its_file_list_then_downloads_just_the_game(fakes):
    client, prowlarr, qb, settings = fakes
    magnet = f"magnet:?xt=urn:btih:{SET_HASH}&dn=Complete+Set"
    prowlarr.results = [release("Pixel Pond (USA)", "http://prowlarr:9696/1/download?file=magnet", seeders=8)]
    prowlarr.torrents["magnet"] = magnet
    qb.magnets[magnet] = SET_TORRENT
    candidate = search(client, settings, POND_GAME)[0]
    status = fetch(client, settings, POND_GAME, candidate, "job-4")

    assert status["state"] == "completed", status
    assert [f["path"] for f in status["files"]] == ["Pixel Pond (USA).gb"]
    assert qb.forms[0]["stopCondition"] == ["MetadataReceived"]
    assert qb.priorities[SET_HASH] == {0: 0, 1: 0, 3: 0}


def test_a_magnet_link_without_the_game_fails_the_fetch(fakes):
    client, prowlarr, qb, settings = fakes
    magnet = f"magnet:?xt=urn:btih:{DISC_HASH}&dn=Pixel+Pond"
    prowlarr.results = [release("Pixel Pond (USA)", "http://prowlarr:9696/1/download?file=magnet", seeders=8)]
    prowlarr.torrents["magnet"] = magnet
    qb.magnets[magnet] = DISC_TORRENT
    candidate = search(client, settings, POND_GAME)[0]
    status = fetch(client, settings, POND_GAME, candidate, "job-5")

    assert status["state"] == "failed"
    assert status["error"] == "The torrent doesn't have a dump of Pixel Pond."
    assert qb.deleted == [(DISC_HASH, "true")]  # added for this fetch, so it goes again


def test_games_sharing_a_torrent_keep_each_others_files_and_the_last_removes_it(fakes):
    client, _, qb, settings = fakes
    retrieve._tidied.add(settings["qbittorrentUrl"])  # running a while: job-0's fetch is another one still going
    settings = {**settings, "afterwards": "remove"}
    info_hash, files = torrent.read(SET_TORRENT)
    # Another game's request added the set, turned off every file but its own, and is still downloading.
    qb.added[info_hash] = {"files": files, "started": False, "body": b"", "tags": {"romdex", "romdex-job-0"}, "metadata": True}
    qb.priorities[info_hash] = {0: 1, 1: 0, 2: 0, 3: 0}
    candidate = search(client, settings, POND_GAME)[0]
    assert fetch(client, settings, POND_GAME, candidate, "job-6")["state"] == "completed"
    assert qb.priorities[info_hash][2] == 1
    assert qb.deleted == []  # the other request is still using it
    assert qb.added[info_hash]["tags"] == {"romdex", "romdex-job-0"}

    qb.added[info_hash]["tags"].discard("romdex-job-0")  # it finishes too
    assert fetch(client, settings, POND_GAME, candidate, "job-7")["state"] == "completed"
    assert qb.deleted == [(info_hash, "true")]


def test_a_torrent_qbittorrent_stops_says_why_and_is_kept_while_another_game_uses_it(fakes):
    client, _, qb, settings = fakes
    retrieve._tidied.add(settings["qbittorrentUrl"])
    info_hash, files = torrent.read(SET_TORRENT)
    qb.added[info_hash] = {"files": files, "started": False, "body": b"", "tags": {"romdex", "romdex-job-0"}, "metadata": True,
                           "state": "error"}
    candidate = search(client, settings, POND_GAME)[0]
    status = fetch(client, settings, POND_GAME, candidate, "job-8")

    assert status["state"] == "failed"
    assert "Permission denied" in status["error"]
    assert "Save folder is a folder qBittorrent has, as qBittorrent sees it (its default is /downloads)" in status["error"]
    assert qb.deleted == [] and qb.added[info_hash]["tags"] == {"romdex", "romdex-job-0"}


def test_a_torrent_qbittorrent_is_still_adding_is_waited_for(fakes):
    client, _, qb, settings = fakes
    qb.unlisted = 3
    candidate = search(client, settings, POND_GAME)[0]
    assert fetch(client, settings, POND_GAME, candidate, "job-9")["state"] == "completed"
    assert "romdex-job-9" not in qb.added[SET_HASH]["tags"]  # tagged once listed, and let go when done


def test_a_torrent_qbittorrent_never_lists_fails_the_fetch(fakes, monkeypatch):
    client, _, qb, settings = fakes
    monkeypatch.setattr(retrieve, "ADD_WAIT_SECONDS", 0.05)
    qb.unlisted = 10**9
    candidate = search(client, settings, POND_GAME)[0]
    status = fetch(client, settings, POND_GAME, candidate, "job-10")
    assert status["state"] == "failed"
    assert "didn't add the torrent" in status["error"]


def test_health_reports_both_services(fakes):
    client, _, _, settings = fakes
    health = client.post("/v1/health", json={"settings": {**settings, "savePath": "/downloads/romdex"}}).json()
    assert health["status"] == "healthy"
    assert health["message"] == "Prowlarr 2.6.5 with 1 indexer, qBittorrent v5.1.4"


def test_health_warns_when_the_save_folder_is_not_under_qbittorrents_own(fakes):
    client, _, qb, settings = fakes
    qb.default_save = "/data/torrents"
    health = client.post("/v1/health", json={"settings": {**settings, "savePath": "/downloads/romdex"}}).json()
    assert health["status"] == "degraded"
    assert ("qBittorrent saves under /data/torrents, but the save folder is /downloads/romdex, which qBittorrent may "
            "not have; try /data/torrents/romdex") in health["message"]
    assert client.post("/v1/health", json={"settings": {**settings, "savePath": "/data/torrents/romdex"}}).json()["status"] == "healthy"


def test_magnet_links_give_their_hash():
    assert torrent.magnet_hash("magnet:?xt=urn:btih:" + "AB" * 20) == "ab" * 20
    assert torrent.magnet_hash("magnet:?xt=urn:btih:VPVPVPVPVPVPVPVPVPVPVPVPVPVPVPVP") == "abeaf" * 8  # the same hash in base32


def test_fetched_files_are_copied_as_reflinks_unless_turned_off(fakes, monkeypatch):
    client, _, _, settings = fakes
    from app import reflinks
    cloned = []
    real = reflinks.clone
    monkeypatch.setattr(reflinks, "clone", lambda source, target: cloned.append(source.name) or real(source, target))
    candidate = search(client, settings, POND_GAME)[0]
    for job, on in [("job-r1", True), ("job-r2", False)]:
        assert fetch(client, {**settings, "reflinks": on}, POND_GAME, candidate, job)["state"] == "completed"
        assert len(cloned) == 1  # the second fetch copied as usual


def test_the_reflink_check_tries_a_downloaded_file_and_says_why_not(fakes, monkeypatch, tmp_path):
    client, _, _, settings = fakes
    from app import reflinks
    check = lambda: client.post("/v1/checks/reflinks", json={"settings": settings}).json()
    (tmp_path / "downloads").mkdir(exist_ok=True)
    assert check() == {"passed": False, "message": "There's nothing in qBittorrent's downloads folder to try it with "
                                                   "yet; check again once something has downloaded."}
    (tmp_path / "downloads" / "romdex").mkdir()
    (tmp_path / "downloads" / "romdex" / "Pixel Pond (USA).gb").write_bytes(b"\0" * 400)

    def different_disks(fd, request, source):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(reflinks.fcntl, "ioctl", different_disks)
    assert check()["message"] == ("From qBittorrent's downloads to staging, files can't share their disk space: "
                                  "they're on different filesystems.")
    monkeypatch.setattr(reflinks.fcntl, "ioctl", lambda fd, request, source: None)
    assert check()["passed"] is True
    assert not list((tmp_path / "staging").iterdir())
    assert client.post("/v1/checks/nothing", json={"settings": settings}).status_code == 404


def test_the_manifest_offers_the_reflink_check_and_searching():
    manifest = TestClient(main.app).get("/v1/manifest").json()
    assert manifest["checks"][0]["id"] == "reflinks"
    assert manifest["capabilities"] == ["search", "cancel"]
    assert manifest["settingsSchema"]["properties"]["reflinks"]["default"] is True
