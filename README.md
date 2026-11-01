# IPTV Browser

A small local web page for browsing free IPTV playlists and playing channels in VLC.
One Python file, no installs, works on Linux, macOS and Windows.

It loads the public [iptv-org](https://github.com/iptv-org/iptv) playlists (US-only or worldwide),
lets you search and filter by genre, check which channels are alive, keep favorites, and open
any channel in VLC with one click.

## Requirements

- Python 3.6 or newer (standard library only, nothing to pip install)
- [VLC](https://www.videolan.org/vlc/)
- Any web browser

## Quick start

```
python3 iptv.py --lite     # text-only list, light on old computers
python3 iptv.py            # card grid with channel logos
```

Your browser opens at `http://127.0.0.1:8765`. Press Ctrl+C in the terminal to stop.

## Options

| Option | What it does |
|---|---|
| `--lite` | Text-only list, no images. Best for weak or old machines. |
| `--port N` | Use another port (default 8765). If it's busy, the next free one is used. |
| `--no-browser` | Don't open a browser automatically. Open the printed address yourself. |
| `--browser NAME` | Use a specific browser, for example `--browser firefox`. |
| `--list LINK_OR_FILE` | Add a playlist at startup. Can be repeated. Saved for next time. |

Environment variables:

| Variable | What it does |
|---|---|
| `VLC_PATH` | Full path to VLC if it isn't found automatically. |
| `IPTV_BROWSER_HOME` | Folder for the cache and settings. |

## Using it

- **Lists:** pick a list from the dropdown. On first run, two starter lists are added for you
  (US channels and Worldwide). After that they are ordinary lists, and nothing is hardcoded.
  Refresh list downloads the latest copy (or re-reads your local file).
- **Add list:** press Add list and enter a web link (`http://` or `https://`) or the full path
  of an `.m3u` file on your computer, then an optional name. The list is tested first and
  rejected if it has no channels. Remove list removes the selected list, including the two
  starters. Removed starters don't come back; add them again with their links if you want them.
  Your favorites are kept. Examples of links to try:
  - `https://iptv-org.github.io/iptv/countries/us.m3u` (United States)
  - `https://iptv-org.github.io/iptv/index.m3u` (worldwide, very large)
  - `https://iptv-org.github.io/iptv/countries/ca.m3u` (Canada)
  - `https://iptv-org.github.io/iptv/categories/news.m3u` (all news channels)
  - `https://iptv-org.github.io/iptv/languages/spa.m3u` (Spanish language)
- **Search and genre:** type to filter by name, or pick a genre from the dropdown.
- **Favorites:** click the star on a channel, then tick Favorites to show only those.
- **Recent:** tick Recent to see the last channels you played, newest first.
- **Check links:** tests the channels currently shown. Green means alive, red means dead.
  Results are remembered for 12 hours. Shift-click to recheck sooner.
  Tick Hide dead to remove dead channels from the list.
- **Tag:** change a channel's genre. Leave the box empty to reset it.
- **Play:** double-click a channel or press Play. VLC opens with the stream.
  If the playlist lists a referrer or user-agent for the stream, they are passed to VLC.

Tip: with the worldwide list, filter by genre before pressing Check links. Checking
thousands of channels takes a long time.

## Where your data is stored

In an `iptv-data` folder next to the script (or `~/.iptv-browser` if that folder isn't writable):

- `us.m3u`, `world.m3u` and one file per web list you added: saved copies of the playlists
  (local `.m3u` files are read from where they are, not copied)
- `state.json`: your added lists, favorites, recent channels, link-check results, custom genres
- `logos/`: cached logo images (normal mode only)

Copy the whole folder to another computer to take your favorites with you.

## Troubleshooting

**"VLC wasn't found."** Install VLC (`sudo apt install vlc` on Debian/Ubuntu), or set `VLC_PATH`.
Flatpak VLC is detected automatically.

**No browser opens, or a keyring or "application not installed" message appears.**
The system's default browser may point to an app that isn't installed. The script tries
installed browsers (Firefox, Chromium, Chrome, Brave, Edge and others) and otherwise prints
the address. Open it in any browser, or use `--browser firefox`.

**A channel doesn't play.** Many free streams go offline, are blocked in your country, or
need special access (HTTP 403 or 404 errors). Use Check links, then Hide dead, and try another channel.

**"Couldn't download the playlist."** Check your internet connection and the link. If you already
have a saved copy, it is used automatically.

**"No channels found in that list."** The link or file isn't a playlist, or it only has non-web
(`http`/`https`) streams. Open it in a text editor and check that it starts with `#EXTM3U`.

**The page says it can't reach the local server.** The script stopped. Start it again.

## Notes

- The server listens only on `127.0.0.1`, so it's reachable from your own computer only.
  It also rejects requests that don't come from its own page.
- This tool only lists and opens streams that are already published in public playlists.
  It doesn't host or copy any video. Make sure what you watch is legal where you live.
- Ubuntu 18.04 and GalliumOS no longer receive security updates. Consider upgrading when you can.