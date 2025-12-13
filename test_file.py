#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

# import your scripts as modules
import yt_dwn              # :contentReference[oaicite:3]{index=3}
import mp3_details         # :contentReference[oaicite:4]{index=4}
import mp3_cover_finder    # :contentReference[oaicite:5]{index=5}
import filename_to_title   # :contentReference[oaicite:6]{index=6}

# These two currently run work at import-time (they’re “script style”),
# so either refactor them into functions or keep them separate.
# import same_cover        # :contentReference[oaicite:7]{index=7}
# import unlink_album_cover# :contentReference[oaicite:8]{index=8}

import shutil

repo_root = Path(__file__).resolve().parent
default_mp3_dir = repo_root / "Music"         # repo-local working folder
final_mp3_dir   = repo_root / "All Music"   # repo-local final folder


def export_music(src_dir: str, dst_dir: str, *, move: bool = True, overwrite: bool = False):
    src = Path(src_dir).expanduser()
    dst = Path(dst_dir).expanduser()
    if not src.is_dir():
        print(f"❌ Source folder not found: {src}")
        return
    dst.mkdir(parents=True, exist_ok=True)

    mp3s = list(src.glob("*.mp3"))
    if not mp3s:
        print("No .mp3 files found to export.")
        return

    count = 0
    skipped = 0
    for f in mp3s:
        out = dst / f.name
        if out.exists() and not overwrite:
            skipped += 1
            continue
        try:
            if move:
                shutil.move(str(f), str(out))
            else:
                shutil.copy2(str(f), str(out))
            count += 1
        except Exception as e:
            print(f"⚠️ Failed exporting {f.name}: {e}")

    action = "Moved" if move else "Copied"
    print(f"✅ {action} {count} file(s) to {dst}")
    if skipped:
        print(f"↩️ Skipped {skipped} existing file(s) (use overwrite to replace).")


def _run_module_main(module, argv):
    """Run an existing script's main() by temporarily swapping sys.argv."""
    old = sys.argv[:]
    try:
        sys.argv = [module.__file__, *argv]
        module.main()
    finally:
        sys.argv = old


def main():
    p = argparse.ArgumentParser(prog="mp3_tool", description="One CLI for your MP3 workflow.")
    sub = p.add_subparsers(dest="cmd", required=False)

    # download (yt_dwn.py)
    sp = sub.add_parser("download", help="YouTube → MP3 downloader")
    sp.add_argument("--from-file", "-f", default="prompts/yt_links.txt",
                    help="Text file with one URL per line (default: prompts/yt_links.txt)")
    sp.add_argument("--outdir", "-o", default=str(default_mp3_dir), help="Output directory")
    sp.add_argument("--kbps", "-k", type=int, default=192, help="MP3 bitrate")
    sp.add_argument("--no-playlist", action="store_true")
    sp.add_argument("--no-thumb", action="store_true")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("urls", nargs="*", help="Optional URLs (if you want to pass them directly)")


    # details (mp3_details.py)
    sp = sub.add_parser("details", help="Fetch/write album/year/genre/artist/title + cover")
    sp.add_argument("args", nargs=argparse.REMAINDER)

    # coverfind (mp3_cover_finder.py) – older/simpler cover+album only
    sp = sub.add_parser("coverfind", help="Cover+album finder (simpler version)")
    sp.add_argument("args", nargs=argparse.REMAINDER)

    # filename-title (filename_to_title.py)
    sp = sub.add_parser("filename-title", help="Set titles from filenames (PDF/DOCX/MP3)")
    sp.add_argument("args", nargs=argparse.REMAINDER)

    # Optional: interactive menu if user runs with no args
    args = p.parse_args()
    if not args.cmd:
        print("Choose a command:")
        print("  1) download")
        print("  2) details")
        print("  3) coverfind")
        print("  4) filename-title")
        print("  5) export (move finalized to another folder)")
        print("  0) Exit")
        choice = input("> ").strip()
        mapping = {"1": "download", "2": "details", "3": "coverfind", "4": "filename-title", "0": "exit", "5": "export"}
        args.cmd = mapping.get(choice, "")
        args.args = []
        args.urls = []
        args.from_file = "prompts/yt_links.txt"
        args.outdir = default_mp3_dir
        args.kbps = 192

    if args.cmd == "download":
        # Works for both CLI subcommand mode and interactive menu mode
        from_file = getattr(args, "from_file", "prompts/yt_links.txt")
        outdir    = getattr(args, "outdir", str(final_mp3_dir))
        kbps      = getattr(args, "kbps", 192)

        urls      = getattr(args, "urls", [])
        # no_pl     = getattr(args, "no_playlist", True)
        no_thumb  = getattr(args, "no_thumb", True)
        # quiet     = getattr(args, "quiet", False)

        forwarded = []
        if urls:
            forwarded += urls
        else:
            forwarded += ["--from-file", from_file]

        forwarded += ["-o", str(outdir), "-k", str(kbps)]
        # if no_pl:    forwarded += ["--no-playlist"]
        if no_thumb: forwarded += ["--no-thumb"]
        # if quiet:    forwarded += ["--quiet"]

        _run_module_main(yt_dwn, forwarded)


    elif args.cmd == "details":
        # ✅ Provide required -p
        args.args = ["-p", str(default_mp3_dir), "--update-album", "--update-year", "--update-genre", "--update-track"]
        _run_module_main(mp3_details, args.args)

    elif args.cmd == "coverfind":
        args.args = ["-p", str(default_mp3_dir)]
        _run_module_main(mp3_cover_finder, args.args)

    elif args.cmd == "filename-title":
        filename_to_title.main(str(default_mp3_dir))

    elif args.cmd == "export":
        dst = input(f"Export to folder (default {final_mp3_dir}): ").strip().strip('"') or str(final_mp3_dir)
        export_music(str(default_mp3_dir), dst, move=True, overwrite=False)


    elif args.cmd == "exit" or args.cmd == "0":
        print("Exiting.")
        return False
    
    else:
        p.print_help()
    return True

if __name__ == "__main__":
    while main():
        pass
