# YT Playlist → Mister Download Manager

Cross-platform PyQt6 app that lists a YouTube playlist with `yt-dlp` metadata and
downloads the selected videos through **Mister Download Manager** (MDM) for
multi-connection transfers.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`
- `certifi` is installed by the requirements file: pip's `yt-dlp` wheel does
  not pull it in, and HTTPS requests fail with `CERTIFICATE_VERIFY_FAILED`
  without it.
- A headless MDM engine. This project ships one in `engine/`:
  - `engine/mdm-video-engine.exe` is already built for Windows x64.
  - It is a small launcher around MDM's `mdm-cli`, built from MDM source.
- A separately installed Mister Download Manager is optional if the bundled
  engine is present: <https://github.com/anefzaoui/misterdownloadmanager/releases>
  - Windows installer locations are auto-detected
    (`%LOCALAPPDATA%\Programs\Mister Download Manager\` and
    `%ProgramFiles%\Mister Download Manager\`).
  - Linux/macOS installers ship `misterdownloadmanager-cli` the same way.
  - Without the bundled engine or an installed MDM, the app falls back to
    yt-dlp's native downloader (slower).
- `ffmpeg` is required for the native engine (merging separate video+audio
  streams and audio conversion). The app auto-detects PATH, a local
  `bin\ffmpeg.exe`, and WinGet/Homebrew locations.
  Windows: `winget install Gyan.FFmpeg`.
- A JavaScript runtime (Deno, Node, Bun or QuickJS) is recommended for the
  native engine: yt-dlp uses it to expose the full set of YouTube formats.
- `yt-dlp` and `ffmpeg` must be visible to the engine. The app adds Python's
  `Scripts` folder and its detected ffmpeg folder to the child process PATH.

## Run

```bat
py -m pip install -r requirements.txt
py main.py
```

On Linux/macOS use `python3 main.py`.

## Using it

1. Paste a playlist (or single video) URL and press **Fetch**.
2. Choose a quality in the top bar. The choice applies to the next batch and is
   saved as the default.
3. Tick the videos you want (or **Select All**) and press **Download Selected**.
4. Watch per-row status, percentage, speed and ETA.
5. **Open Folder** reveals the playlist folder.

Video presets cap the height at 1080p, 720p, 360p, 240p or 144p. A request for
280p is treated as 240p, since YouTube's practical ladder does not offer 280p.
Audio presets are Original, MP3 320, Opus and FLAC.

## Mister Download Manager integration

MDM is used through its documented headless CLI, **not** yt-dlp's
`--external-downloader` flag (yt-dlp only accepts
aria2c/axel/curl/wget/httpie/ffmpeg there and rejects unknown executables).

The app detects, in order:

1. The path saved in Settings.
2. The bundled `engine/mdm-video-engine.exe`.
3. An installed MDM CLI or app.
4. yt-dlp's native downloader as a fallback.

Three engines, switchable in Settings:

- **MDM video-get (default)** – `--json video-get URL -f FMT -o DIR -c N`.
  MDM resolves the media, downloads each stream with N connections, merges
  audio/video and reports JSON progress. Most robust.
- **MDM direct URL (experimental)** – the app resolves a single-file media URL
  with yt-dlp, then runs `--json get URL -o FILE -c N -H ...`.
  Raw segmented transfer; HLS/DASH and expiring YouTube URLs are not supported
  here, and YouTube often has no combined single file. Direct mode only supports
  **Audio only (Original)**; it cannot convert to MP3, Opus or FLAC.
- **yt-dlp built-in** – yt-dlp downloads directly (one connection per video,
  slower). Useful for private videos with cookies, or when MDM is unavailable.
  Without ffmpeg it can only download combined video formats, and audio
  conversion is disabled.

If the engine and installed MDM are missing or fail validation, the app switches
to the yt-dlp engine automatically and shows a warning banner.

### Changing the MDM path manually

Auto-detection covers the bundled engine, standard installer locations, PATH,
portable folders and AppImage names. If your install is elsewhere:
**Settings → Mister Download Manager CLI → Browse…**, pick
`mdm-video-engine.exe`, `misterdownloadmanager-cli.exe` or
`misterdownloadmanager.exe`; the app finds the sibling CLI or uses `--cli`, then
press **Validate**.

You can also edit `%APPDATA%\YTPlaylistMDM\config.json` (Windows),
`~/Library/Application Support/YTPlaylistMDM/config.json` (macOS),
`~/.config/YTPlaylistMDM/config.json` (Linux) and set `"mdm_cli"` to the full
path.

## Building the bundled engine

The checked-in Windows binary was built from MDM commit
`9eac50c6a9205bcfcf741b58a439aed8fee75f11`. To rebuild it:

```powershell
.\engine\build-engine.ps1
```

The script copies `engine/src/mdm-video-engine.rs` into an MDM checkout as
`crates/cli/src/bin/mdm-video-engine.rs`, runs:

```powershell
cargo build --release -p mdm-cli --bin mdm-video-engine
```

then copies the executable, `LICENSE` and build metadata into `engine/`.
Use `-MdmSource <path>` if the MDM checkout is not the sibling
`..\misterdownloadmanager` folder. See `engine/README.md` for toolchain details.

## Notes

- Concurrency defaults to 2 videos at once; MDM uses 8 connections per video
  (`-c`, up to 128). Sequential is often enough because of `-c`.
- Cancelled/failed MDM downloads keep their `.mdm.json` sidecar so MDM can resume.
- Private/age-restricted videos may need cookies: set `cookies_from_browser` in
  Settings (e.g. `chrome`, `firefox`, `chrome:Profile 1`). Cookies are used for
  metadata and by the native/direct engines; MDM video-get uses its own setup.
  If the browser locks its cookie database (Windows), the app automatically
  retries without cookies and says so in the status bar; close the browser to
  use cookies.
- Audio conversion is done by MDM video-get or by yt-dlp + ffmpeg in native
  mode. Direct mode rejects MP3, Opus and FLAC.
- Native mode without ffmpeg cannot merge separate streams, so most YouTube
  videos fail; the app warns before starting. Install ffmpeg
  (`winget install Gyan.FFmpeg`) or use the video-get engine.

## License

The bundled engine is built from Mister Download Manager and is distributed
under GPL-3.0-or-later. See `engine/LICENSE` and `engine/BUILD.txt`.
