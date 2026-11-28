# Changelog

What changed in each release of this plugin. Versions follow [semantic versioning](https://semver.org).

## [Unreleased]

- A game is looked for on its own system. Torrents named for another system, such as a Switch game's `[NSP]`, rank
  far below the game's own, and sequels and other games of a series below the game itself: searching for The Legend
  of Zelda on the NES no longer turns up Breath of the Wild, as a magnet link could.
- Searches also ask for the game's name with its system's and for the system's name, and read the best-seeded few
  torrents named for the system, so a whole set that doesn't name the game is found.
- Eight torrents' file lists are read per search, where it was six, and a file only another system has isn't offered
  for being named like the game.

## [1.0.0]

- The first release: searches the torrent indexers you've set up in Prowlarr for a game, offers torrents with a file
  named as the DAT names one of its dumps, and downloads just that dump's files with your qBittorrent. Its settings
  are those of Melodex's Prowlarr plugin, but for the ones only music has.
