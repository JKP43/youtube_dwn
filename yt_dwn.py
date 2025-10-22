#!/usr/bin/env python3
"""
YouTube → MP3 downloader (single videos or playlists)

Features:
- Downloads best available audio and converts to MP3 (default 192 kbps)
- Writes ID3 metadata (title/artist/album) and embeds thumbnail cover art
- Works with single URLs, playlists, or a text file of URLs
- Skips files that already exist (by ID) to avoid duplicates
- Simple progress display and clear error messages

Usage examples:
  python ytmp3.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"
  python ytmp3.py "https://www.youtube.com/playlist?list=YYYYYYYYYYY"
  python ytmp3.py --from-file urls.txt -k 192 -o "Downloads/MP3s"
"""

import argparse
import sys
import textwrap
from pathlib import Path
from typing import List, Dict, Any

try:
    from yt_dlp import YoutubeDL
except Exception as e:
    print("Error: yt-dlp is not installed. Install with: pip install yt-dlp", file=sys.stderr)
    raise

# -------- CLI --------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ytmp3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Download YouTube audio as MP3 with metadata & thumbnail.",
        epilog=textwrap.dedent("""
            Notes:
              • Please download only content you have rights to.
              • Requires ffmpeg installed and on PATH.
        """),
    )
    p.add_argument("urls", nargs="*", help="Video or playlist URLs.")
    p.add_argument("--from-file", "-f", dest="from_file", help="Path to a text file with one URL per line.")
    p.add_argument("--outdir", "-o", default="mp3_downloads", help="Output directory (default: mp3_downloads).")
    p.add_argument("--kbps", "-k", type=int, default=192, help="MP3 bitrate in kbps (default: 192).")
    p.add_argument("--no-playlist", action="store_true",
                   help="Treat input as single videos only (ignore playlist expansion).")
    p.add_argument("--no-thumb", action="store_true", help="Do not download/embed thumbnails.")
    p.add_argument("--quiet", action="store_true", help="Less verbose output.")
    return p.parse_args()

# -------- Progress Hook --------
def progress_hook(d: Dict[str, Any]):
    if d.get("status") == "downloading":
        # Simple percent display
        p = d.get("_percent_str", "").strip()
        spd = d.get("_speed_str", "").strip()
        eta = d.get("_eta_str", "").strip()
        msg = f"Downloading: {p}  Speed: {spd}  ETA: {eta}"
        print(msg, end="\r", flush=True)
    elif d.get("status") == "finished":
        print("Download complete. Converting…        ")

# -------- Main --------
def load_urls(args: argparse.Namespace) -> List[str]:
    urls = list(args.urls) if args.urls else []
    if args.from_file:
        fp = Path(args.from_file)
        if not fp.exists():
            print(f"Error: URL file not found: {fp}", file=sys.stderr)
            sys.exit(1)
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    if not urls:
        print("No URLs provided. See --help for usage.", file=sys.stderr)
        sys.exit(1)
    return urls

def main():
    args = parse_args()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    # Template includes video ID so re-runs don't duplicate
    outtmpl = str(outdir / "%(title)s [%(id)s].%(ext)s")

    postprocessors = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(args.kbps),
        },
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]
    write_thumb = not args.no_thumb
    if write_thumb:
        # yt-dlp embeds the downloaded thumbnail for MP3 via ffmpeg where possible
        postprocessors.append({"key": "EmbedThumbnail"})

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "ignoreerrors": True,      # continue on individual failures
        "noplaylist": args.no_playlist,
        "writethumbnail": write_thumb,
        "postprocessors": postprocessors,
        "progress_hooks": [progress_hook],
        "overwrites": False,       # don't clobber existing files
        "concurrent_fragment_downloads": 4,
        "quiet": args.quiet,
        "no_warnings": args.quiet,
        # Tidy up filenames
        "restrictfilenames": True,
        "trim_file_name": 200,
    }

    urls = load_urls(args)

    legal_notice = (
        "Legal reminder: download only content you have rights to "
        "(your uploads, public-domain, or appropriately licensed)."
    )
    if not args.quiet:
        print(legal_notice)
        print(f"Output: {outdir}")
        print(f"Bitrate: {args.kbps} kbps")
        if write_thumb:
            print("Thumbnail: will embed cover art")
        if args.no_playlist:
            print("Playlist handling: disabled (treat as single videos)")
        print("-" * 60)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ret = ydl.download(urls)
    except FileNotFoundError:
        print(
            "Error: ffmpeg not found. Please install ffmpeg and ensure it is on your PATH.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

    # yt-dlp returns 0 on success, 1 on partial errors; we already printed issues as they occurred
    if ret == 0:
        print("\nAll done ✅")
    else:
        print("\nCompleted with some errors (see above).")

if __name__ == "__main__":
    main()
