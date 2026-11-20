# Prowlarr retrieval plugin

A Romdex retrieval plugin (contract v1.0) that searches the torrent indexers you've set up in
[Prowlarr](https://prowlarr.com) and downloads with your [qBittorrent](https://www.qbittorrent.org). What you can find
is what your indexers offer; it's up to you to use ones whose games you're allowed to download, such as homebrew and
games their makers have released for free.

It runs beside [Romdex](https://github.com/Dextrd/Romdex) as a container of its own, which Romdex reaches over the
[retriever contract](https://github.com/Dextrd/Romdex/blob/main/docs/guide/writing-a-retriever-plugin.md).

## What it does

- **Search.** Asks Prowlarr's console categories for the game's name. The best-named torrents have their .torrent file
  read, and a torrent is offered only when it has one of the game's dumps, as the DAT names them:
  - its own files by name, such as `Pixel Pond (USA).gb`, or a disc's cue sheet and every track; with the DAT's sizes
    too, that's a score of 1;
  - an archive or a CHD named after the dump, such as `Pixel Pond (USA).7z`, which Romdex reads inside;
  - failing those, the file named most like the dump, if it's at least as alike as **Lowest match score**.

  A torrent of a whole system's set is offered for the one game's files. The dump you asked for is the one looked
  for; otherwise any, the one your preferences put first winning a tie. Torrents that only come as a magnet link
  can't be read ahead, so they rank lower and are checked once qBittorrent has their file list. Between equally good
  matches, the better-seeded comes first.
- **Fetch.** Adds the torrent to qBittorrent stopped, with the `romdex` category and tag, turns off every file that
  wasn't asked for (the rest of a set, scans, readmes), starts it, and reports progress. When the files are done, it
  copies them to staging, leaving qBittorrent to seed: a dump of several files, such as a disc, in a folder of its
  own. A torrent that was already in qBittorrent keeps its files; the requested ones are added to it. Copies are
  reflinks where the filesystem can make them (see below). Romdex then works out what arrived from its checksums,
  whatever the torrent called it, and files only what it can verify.
  While it runs, the torrent also carries a `romdex-<job>` tag, so games sharing a torrent know who's still using
  it; the tag is deleted once the fetch is done. The first time the plugin reaches qBittorrent after starting, it
  deletes any such tags a fetch cut short left behind. The `romdex` tag stays: it marks the torrents Romdex added.
- **Cancel.** Removes a torrent it added, with what it downloaded. One that was already there stays.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `prowlarrUrl` | `http://prowlarr:9696` | Where this plugin reaches Prowlarr. |
| `prowlarrApiKey` | | In Prowlarr under Settings → General. Needed. |
| `qbittorrentUrl` | `http://qbittorrent:8080` | qBittorrent's Web UI. |
| `qbittorrentUsername`, `qbittorrentPassword` | | Leave empty when qBittorrent lets this plugin in without logging in. |
| `category` | `romdex` | The qBittorrent category torrents are added with. |
| `savePath` | `/downloads/romdex` | Where qBittorrent saves them, as qBittorrent sees it. |
| `downloadsFolder` | | The same folder as this plugin sees it, when it's mounted somewhere else. |
| `minSeeders` | `1` | Torrents with fewer seeders aren't offered. |
| `minScore` | `0.6` | Torrents that match less well than this aren't offered. A file named as the DAT names a dump always is. |
| `afterwards` | `keep` | `keep` leaves the torrent seeding; `remove` takes it out, with its files, once Romdex has a copy. |
| `reflinks` | on | Copies files as reflinks, so games you keep seeding aren't stored twice. Where they can't be made, files are copied as usual. |

They're the settings of [Melodex's Prowlarr plugin](https://github.com/Dextrd/melodex-retriever-prowlarr), but for
the ones only music has (`format` and `losslessTrackers`).

## Adding it to Romdex

Add this under `services:` in the compose file you run Romdex with, beside `core` and `igir-worker`, so it shares
their `staging` volume:

```yaml
  retriever-prowlarr:
    image: ghcr.io/dextrd/romdex-retriever-prowlarr:latest
    container_name: romdex-retriever-prowlarr
    user: "${PUID:-1000}:${PGID:-1000}"
    restart: unless-stopped
    volumes:
      - staging:/staging
      - ${DOWNLOADS_PATH:-./downloads}:/downloads:ro
```

`DOWNLOADS_PATH` is qBittorrent's downloads folder, which qBittorrent mounts at `/downloads` too; set it in the `.env`
file beside your compose file, or write the path in its place.

Then start it with `docker compose up -d`. `latest` follows each release; to choose when it changes, use a version
such as `1` or `1.0.0` instead. On SELinux hosts with podman, give it the same `security_opt` as Romdex's own
containers.

Where qBittorrent mounts it somewhere else, say the pool at `/data` with torrents in `/data/torrents`, set **Save
folder** to the path qBittorrent sees (`/data/torrents/romdex`) and **Save folder for this plugin** to the path the
plugin sees (`/downloads/romdex`). The health check warns when **Save folder** isn't under qBittorrent's own default
save path, as qBittorrent can't write anywhere it doesn't have: fetches then fail with "Permission denied".

Then add `http://retriever-prowlarr:8080` under Settings → Plugins in Romdex (it's one of the templates under Add),
and fill in Prowlarr's API key in its settings.
Environment variables: `STAGING_ROOT` (default `/staging`) and `PARALLEL_FETCHES` (default `2`).

## Sharing disk space with what you seed

A game you keep seeding would otherwise be on disk twice: in qBittorrent's downloads and in your library. A reflink
is a separate file that shares its data on disk with the original until one of them is changed, so it takes next to
no space, and nothing Romdex or igir does to its copy touches the file qBittorrent seeds.

The plugin copies into staging as reflinks. That needs:

- btrfs, XFS made with reflink support, or ZFS 2.2 or later. Not ext4, network shares, or unRAID's `/mnt/user`.
- qBittorrent's downloads and the staging volume on that same filesystem. Make `staging` a folder on it rather than a
  volume Docker manages.

**Check reflinks** in the plugin's settings tries it, with a file qBittorrent has downloaded, and says whether it works
and why not. Where reflinks can't be made, everything still works, with ordinary copies.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

The tests answer for Prowlarr and qBittorrent with fakes, so they don't need either.
