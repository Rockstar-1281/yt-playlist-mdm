# mdm-video-engine

Headless video engine for **YT Playlist → Mister Download Manager**.

`mdm-video-engine.exe` is a thin launcher around Mister Download Manager's
`mdm-cli` library. It exposes the same JSON protocol used by the app:

```text
mdm-video-engine.exe --json video-get URL -f SELECTOR -o DIR -c N
mdm-video-engine.exe --json get URL -o FILE -c N [-H "Header: value"]
```

The executable links the full `mdm-cli` code path, including the torrent code,
so it is larger than a video-only build. It requires no installed MDM app.

## Layout

- `src/mdm-video-engine.rs` – canonical Rust source.
- `mdm-video-engine.exe` – Windows x64 release build.
- `build-engine.ps1` – copies the source into an MDM checkout, builds, and
  refreshes the bundled binary.
- `BUILD.txt` – source commit, toolchain versions, size and SHA-256.
- `LICENSE` – GPL-3.0-or-later from the MDM source tree.

## Build

Prerequisites:

- Rust 1.98+ with the MSVC toolchain (`rustup default stable-msvc`).
- Visual Studio 2022 Build Tools with the C++ workload and CMake.
- NASM on PATH.
- An MDM source checkout at `..\misterdownloadmanager` or passed explicitly.

```powershell
.\build-engine.ps1
.\build-engine.ps1 -MdmSource C:\src\misterdownloadmanager
```

The script runs:

```powershell
cargo build --release -p mdm-cli --bin mdm-video-engine
```

Cargo auto-discovers `crates/cli/src/bin/mdm-video-engine.rs`. If a future MDM
layout disables auto-discovery, add an explicit `[[bin]]` entry to
`crates/cli/Cargo.toml`.

The resulting binary is written to `target\release\mdm-video-engine.exe` and
copied back here. The app only needs this folder at runtime.

## Runtime helpers

The engine calls `yt-dlp` and `ffmpeg`. It searches PATH and MDM's standard
helper locations, including Python's `Scripts` folders and WinGet links. The
app also prepends Python's `Scripts` folder and its detected ffmpeg folder to
the child PATH. `ffmpeg` is still required for merging separate video/audio
streams and for MP3, Opus and FLAC conversion. When yt-dlp is pip-installed,
install `certifi` too, or HTTPS requests fail with `CERTIFICATE_VERIFY_FAILED`.

## License and source

Mister Download Manager is GPL-3.0-or-later. The engine binary is distributed
under the same license. The corresponding source is the public MDM repository
at the commit recorded in `BUILD.txt`, plus `src/mdm-video-engine.rs` in this
folder. See `LICENSE`.
