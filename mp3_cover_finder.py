
#!/usr/bin/env python3
"""
MP3 Cover & Album Finder (no API keys)

What's new in this build
- Works even when you only have Title + Contributing Artist (no album).
- Pulls album name from iTunes or MusicBrainz when possible and writes TALB.
- Flags:
  --update-album        write album when discovered (default: only if missing)
  --force               also overwrites existing cover and (if --update-album) album
  --dry-run             preview actions without writing

Sources
- iTunes Search API (free, no key) for art + album name
- MusicBrainz (recording->release) + Cover Art Archive for art + album name
"""

import argparse
import concurrent.futures
import io
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

import requests
from mutagen.id3 import ID3, APIC, TALB, TPE1, TPE2, TIT2, ID3NoHeaderError, error as ID3Error

# -------- Config --------
USER_AGENT = "MP3CoverFinder/1.1 (+https://example.local)"
ITUNES_SEARCH = "https://itunes.apple.com/search"
MB_BASE = "https://musicbrainz.org/ws/2"

def sleep_backoff(base=0.5, factor=1.7, jitter=0.3, attempt=0):
    t = base * (factor ** attempt) + random.uniform(0, jitter)
    time.sleep(min(t, 5.0))

# -------- Utilities --------

def human_bytes(n: int) -> str:
    if n is None:
        return "?"
    f = float(n)
    for unit in ["B","KB","MB","GB"]:
        if f < 1024.0 or unit == "GB":
            return f"{f:.1f}{unit}"
        f /= 1024.0

def http_get(url: str, params: Dict[str, Any] = None, headers: Dict[str, str] = None, timeout=12, stream=False, max_attempts=3):
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    for attempt in range(max_attempts):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout, stream=stream)
            if r.status_code in (429, 500, 502, 503, 504):
                sleep_backoff(attempt=attempt)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == max_attempts - 1:
                raise
            sleep_backoff(attempt=attempt)
    raise RuntimeError("Unreachable")

# -------- ID3 helpers --------

@dataclass
class TrackMeta:
    artist: Optional[str]
    album: Optional[str]
    title: Optional[str]

def read_id3_meta(path: Path) -> TrackMeta:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        # No tag yet
        return TrackMeta(None, None, path.stem)
    except Exception:
        return TrackMeta(None, None, path.stem)

    def get_text(frame_id: str) -> Optional[str]:
        f = tags.getall(frame_id)
        for x in f:
            try:
                if x.text and x.text[0]:
                    return str(x.text[0]).strip()
            except Exception:
                continue
        return None

    artist = get_text("TPE1") or get_text("TPE2")
    album = get_text("TALB")
    title = get_text("TIT2") or path.stem
    return TrackMeta(artist, album, title)

def has_embedded_art(path: Path) -> bool:
    try:
        tags = ID3(path)
        return any(tags.getall("APIC"))
    except Exception:
        return False

def write_album_tag(path: Path, album: str, force=False) -> bool:
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        existing = None
        for t in tags.getall("TALB"):
            try:
                existing = t.text[0]
                break
            except Exception:
                pass

        if existing and not force:
            # Do not overwrite unless forced
            return False

        tags.delall("TALB")
        tags.add(TALB(encoding=3, text=album))
        tags.save(path, v2_version=3)
        return True
    except Exception:
        return False

def embed_cover(path: Path, image_bytes: bytes, mime: str, force_id3v24=False) -> bool:
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        if not mime or "/" not in mime:
            mime = "image/jpeg"
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Front cover", data=image_bytes))
        tags.save(path, v2_version=4 if force_id3v24 else 3)
        return True
    except ID3Error:
        return False

# -------- Fetchers --------

def upscale_itunes_art(url: str, target: int = 1200) -> str:
    # iTunes artwork URLs have size in the path, e.g. /100x100bb.jpg
    return re.sub(r"/\d+x\d+bb\.", f"/{target}x{target}bb.", url)

def itunes_search(artist: Optional[str], album: Optional[str], title: Optional[str]):
    """Return list of dicts with keys: image_bytes, content_type, source, album_title"""
    candidates = []
    queries = []
    if album and artist:
        queries.append({"term": f"{artist} {album}", "entity": "album", "limit": 5})
    if title and artist:
        queries.append({"term": f"{artist} {title}", "entity": "song", "limit": 5})
    if album:
        queries.append({"term": f"{album}", "entity": "album", "limit": 5})
    if title:
        queries.append({"term": f"{title}", "entity": "song", "limit": 5})

    for q in queries:
        try:
            r = http_get(ITUNES_SEARCH, params={"media": "music", **q})
            data = r.json()
        except Exception:
            continue
        for item in data.get("results", []):
            art_url = item.get("artworkUrl100")
            if not art_url:
                continue
            album_title = item.get("collectionName")
            for size in (1200, 1000, 800, 600):
                u = upscale_itunes_art(art_url, size)
                try:
                    img = http_get(u, stream=True)
                    ct = img.headers.get("Content-Type", "").lower()
                    if "image" not in ct:
                        continue
                    content = img.content
                    if len(content) < 25_000:
                        continue
                    candidates.append({
                        "image_bytes": content,
                        "content_type": ct,
                        "source": f"iTunes {size}px",
                        "album_title": album_title
                    })
                    break  # keep the first acceptable size for this item
                except Exception:
                    continue
    return candidates

def mb_find_release_by_artist_title(artist: Optional[str], title: Optional[str]) -> Optional[Tuple[str, str]]:
    """Search MusicBrainz recording by artist+title, return (release_mbid, release_title)."""
    if not (artist and title):
        return None
    try:
        params = {"query": f'artist:"{artist}" AND recording:"{title}"', "fmt": "json", "limit": 1, "inc": "releases"}
        r = http_get(f"{MB_BASE}/recording", params=params, headers={"Accept": "application/json"})
        data = r.json()
        recs = data.get("recordings") or []
        if not recs:
            return None
        rels = recs[0].get("releases") or []
        if not rels:
            return None
        release = rels[0]
        return release.get("id"), release.get("title")
    except Exception:
        return None

def mb_find_release_by_album_artist(artist: Optional[str], album: Optional[str]) -> Optional[Tuple[str, str]]:
    """Search MusicBrainz release by album (and optional artist)."""
    if not album:
        return None
    try:
        if artist:
            q = f'artist:"{artist}" AND release:"{album}"'
        else:
            q = f'release:"{album}"'
        params = {"query": q, "fmt": "json", "limit": 1}
        r = http_get(f"{MB_BASE}/release", params=params, headers={"Accept": "application/json"})
        data = r.json()
        rels = data.get("releases") or []
        if not rels:
            return None
        rel = rels[0]
        return rel.get("id"), rel.get("title")
    except Exception:
        return None

def caa_fetch_front(mbid: str) -> Optional[Tuple[bytes, str]]:
    # Use JSON to pick large thumbnails when possible
    try:
        r = http_get(f"https://coverartarchive.org/release/{mbid}", headers={"Accept": "application/json"})
        data = r.json()
        images = data.get("images", [])
        fronts = [img for img in images if img.get("front")] or images
        urls = []
        for img in fronts:
            thumbs = img.get("thumbnails") or {}
            # prefer large -> small -> original
            for k in ("large", "small"):
                if thumbs.get(k):
                    urls.append(thumbs[k])
            if img.get("image"):
                urls.append(img["image"])
        for u in urls:
            try:
                im = http_get(u, stream=True)
                ct = im.headers.get("Content-Type", "").lower()
                if "image" not in ct:
                    continue
                content = im.content
                if len(content) < 20_000:
                    continue
                return content, ct
            except Exception:
                continue
    except Exception:
        pass
    # final fallback
    try:
        im = http_get(f"https://coverartarchive.org/release/{mbid}/front", stream=True)
        ct = im.headers.get("Content-Type", "").lower()
        if "image" in ct:
            return im.content, ct
    except Exception:
        return None
    return None

def find_cover_and_album(meta: TrackMeta):
    """Return (image_bytes, content_type, source, album_title) or None."""
    # iTunes first
    it_cands = itunes_search(meta.artist, meta.album, meta.title)
    for c in it_cands:
        if c["image_bytes"]:
            return c["image_bytes"], c["content_type"], c["source"], c.get("album_title")

    # MusicBrainz paths
    mb = None
    album_title_from_mb = None
    # Prefer explicit album search if album given
    mb = mb_find_release_by_album_artist(meta.artist, meta.album)
    if not mb and (meta.artist and meta.title):
        mb = mb_find_release_by_artist_title(meta.artist, meta.title)
    if mb:
        mbid, album_title_from_mb = mb
        caa = caa_fetch_front(mbid)
        if caa:
            img_bytes, ct = caa
            return img_bytes, ct, "CoverArtArchive", album_title_from_mb

    return None

# -------- Pipeline --------

@dataclass
class WorkResult:
    path: Path
    status: str
    source: Optional[str] = None
    detail: Optional[str] = None
    bytes_written: Optional[int] = None
    album_set: Optional[bool] = None
    album_value: Optional[str] = None

def process_file(path: Path, args) -> WorkResult:
    try:
        if path.suffix.lower() != ".mp3":
            return WorkResult(path, "skip", detail="not mp3")

        # We will still attempt album update even if art exists when requested
        already_has_art = has_embedded_art(path)

        # Extract minimal meta; support "Artist - Title" filename heuristic
        meta = read_id3_meta(path)
        if not (meta.artist or meta.title or meta.album):
            m = re.match(r"(.+?)\s*-\s*(.+)$", path.stem)
            if m:
                meta = TrackMeta(artist=m.group(1).strip(), album=None, title=m.group(2).strip())

        found = find_cover_and_album(meta)

        album_set_flag = False
        album_value = None
        if found:
            _, _, _, album_title = found
            album_value = album_title
            if album_title and (args.update_album or not meta.album):
                album_set_flag = write_album_tag(path, album_title, force=args.force)

        if args.dry_run:
            if not found:
                # Still report album attempt when not found
                return WorkResult(path, "miss", detail="no cover/album found", album_set=False)
            img_bytes, mime, source, _ = found
            action = "keep" if already_has_art and not args.force else "embed"
            return WorkResult(path, "found", source=source, bytes_written=len(img_bytes), album_set=album_set_flag, album_value=album_value)

        # If we already have art and not forcing, maybe skip embedding
        if already_has_art and not args.force:
            status = "skip"
            detail = "already has art"
        else:
            if not found:
                return WorkResult(path, "miss", detail="no cover/album found", album_set=album_set_flag)
            img_bytes, mime, source, _ = found
            ok = embed_cover(path, img_bytes, mime, force_id3v24=args.id3v24)
            status = "ok" if ok else "error"
            detail = None if ok else "embed failed"

        return WorkResult(path, status, source=found[2] if found else None, detail=detail, bytes_written=(len(found[0]) if found else None), album_set=album_set_flag, album_value=album_value)
    except Exception as e:
        return WorkResult(path, "error", detail=str(e))

def iter_mp3s(root: Path, recursive: bool):
    if recursive:
        yield from (p for p in root.rglob("*.mp3") if p.is_file())
    else:
        yield from (p for p in root.glob("*.mp3") if p.is_file())

def main():
    parser = argparse.ArgumentParser(description="Find & embed cover art; optionally set Album tag.")
    parser.add_argument("-p", "--path", type=str, required=True, help="Folder containing MP3 files")
    parser.add_argument("-r", "--recursive", action="store_true", help="Scan subfolders")
    parser.add_argument("-n", "--concurrency", type=int, default=4, help="Parallel workers (not used in this simplified runner)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing cover art and (with --update-album) album")
    parser.add_argument("--dry-run", action="store_true", help="Search & report only; do not embed")
    parser.add_argument("--id3v24", action="store_true", help="Save tags as ID3v2.4 (default v2.3)")
    parser.add_argument("--update-album", action="store_true", help="Write album tag when discovered (default writes only if missing)")
    args = parser.parse_args()

    root = Path(args.path).expanduser()
    if not root.exists():
        print(f"[!] Path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    files = list(iter_mp3s(root, args.recursive))
    if not files:
        print("[i] No MP3 files found.")
        return

    print(f"[i] Processing {len(files)} file(s) in {root} (recursive={args.recursive}) dry_run={args.dry_run} force={args.force} update_album={args.update_album}")

    ok = sk = miss = err = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(process_file, p, args): p for p in files}
        for fut in as_completed(futs):
            res = fut.result()
            if res.status == "ok":
                ok += 1
                extra = f", album={'set' if res.album_set else 'kept'}" if res.album_set is not None else ""
                if res.album_value:
                    extra += f" ('{res.album_value}')"
                print(f"[OK] {res.path} ({res.source}, wrote {res.bytes_written} bytes){extra}")
            elif res.status == "found":
                extra = f", album would write '{res.album_value}'" if res.album_value else ""
                print(f"[FOUND] {res.path} ({res.source}, would embed {res.bytes_written} bytes{extra})")
            elif res.status == "skip":
                sk += 1
                extra = f"; album set to '{res.album_value}'" if res.album_set else ""
                print(f"[SKIP] {res.path} ({res.detail}){extra}")
            elif res.status == "miss":
                miss += 1
                print(f"[MISS] {res.path} ({res.detail})")
            else:
                err += 1
                print(f"[ERR] {res.path} ({res.detail})")

    print(f"\n[i] Done. ok={ok} skip={sk} miss={miss} err={err} of {len(files)}")

if __name__ == "__main__":
    main()
