
#!/usr/bin/env python3
"""
MP3 Cover & Album Finder (no API keys)

What's new in this build
- Works even when you only have Title + Contributing Artist (no album).
- Pulls album name from iTunes or MusicBrainz when possible and writes TALB.
- Now ALSO fetches and can write Year/Date (TDRC) and Genre (TCON) and, optionally, Artist (TPE1) and Title (TIT2).
- Flags:
  --update-album        write album when discovered (default: only if missing)
  --update-year         write year/date (TDRC) when discovered (default: only if missing)
  --update-genre        write genre (TCON) when discovered (default: only if missing)
  --update-artist       write artist (TPE1) when discovered (default: only if missing)
  --update-title        write title (TIT2) when discovered (default: only if missing)
  --force               also overwrites existing cover and (if --update-*) tags
  --dry-run             preview actions without writing

Sources
- iTunes Search API (free, no key) for art + album name + release date + primary genre
- MusicBrainz (recording/release) + Cover Art Archive for art + album name + date + genres
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
from typing import Optional, Tuple, Dict, Any, List

import requests
from mutagen.id3 import (
    ID3, APIC, TALB, TPE1, TPE2, TIT2, TCON, TDRC, TRCK,
    ID3NoHeaderError, error as ID3Error
)

# -------- Config --------
USER_AGENT = "MP3CoverFinder/1.2 (+https://example.local)"
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
                # small backoff; also respect Retry-After if present
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        time.sleep(min(5.0, float(ra)))
                    except Exception:
                        pass
                else:
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

def _write_text_frame(path: Path, frame_cls, value: str, force=False) -> bool:
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        existing = None
        for t in tags.getall(frame_cls.__name__):
            try:
                existing = t.text[0]
                break
            except Exception:
                pass

        if existing and not force:
            return False

        tags.delall(frame_cls.__name__)
        tags.add(frame_cls(encoding=3, text=value))
        tags.save(path, v2_version=3)
        return True
    except Exception:
        return False

def write_album_tag(path: Path, album: str, force=False) -> bool:
    return _write_text_frame(path, TALB, album, force)

def write_year_tag(path: Path, year_or_date: str, force=False) -> bool:
    # TDRC can store YYYY or YYYY-MM or YYYY-MM-DD
    return _write_text_frame(path, TDRC, year_or_date, force)

def write_genre_tag(path: Path, genre: str, force=False) -> bool:
    return _write_text_frame(path, TCON, genre, force)

def write_artist_tag(path: Path, artist: str, force=False) -> bool:
    return _write_text_frame(path, TPE1, artist, force)

def write_title_tag(path: Path, title: str, force=False) -> bool:
    return _write_text_frame(path, TIT2, title, force)

def write_track_tag(path: Path, track: str, force=False) -> bool:
    return _write_text_frame(path, TRCK, track, force)

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
    """Return list of dicts with keys: image_bytes, content_type, source, album_title, release_date, genre, artist_name, track_title"""
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

            # unify fields for album/song entities
            album_title = item.get("collectionName")
            release_date = (item.get("releaseDate") or "")[:10] or None  # YYYY-MM-DD
            genre = item.get("primaryGenreName")
            artist_name = item.get("artistName")
            track_title = item.get("trackName") or title
            track_number = item.get("trackNumber")
            track_count = item.get("trackCount")

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
                        "album_title": album_title,
                        "release_date": release_date,
                        "genre": genre,
                        "artist_name": artist_name,
                        "track_title": track_title,
                        "track_number": track_number,
                        "track_count": track_count
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

def mb_fetch_release_details(mbid: str) -> Dict[str, Any]:
    """
    Fetch release details including date and genres/tags.
    Returns dict with keys: release_date (YYYY[-MM[-DD]] or None), genres (list of str)
    """
    result = {"release_date": None, "genres": []}
    try:
        # Try genres first (newer MB schema). Fallback to tags if no genres.
        params = {"fmt": "json", "inc": "genres+tags"}
        r = http_get(f"{MB_BASE}/release/{mbid}", params=params, headers={"Accept": "application/json"})
        data = r.json()
        result["release_date"] = data.get("date")  # can be YYYY or YYYY-MM or YYYY-MM-DD
        genres = []
        if "genres" in data and data["genres"]:
            genres = [g.get("name") for g in data["genres"] if g.get("name")]
        if not genres:
            # Fallback to tags; choose highest-count tag first
            tags = data.get("tags") or []
            tags = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
            genres = [t.get("name") for t in tags if t.get("name")]
        result["genres"] = genres
    except Exception:
        pass
    return result

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

def find_cover_and_details(meta: TrackMeta) -> Optional[Dict[str, Any]]:
    """
    Return dict with keys:
      image_bytes, content_type, source,
      album_title, release_date, genre (single), genres (list),
      artist_name, track_title
    """
    # iTunes first
    it_cands = itunes_search(meta.artist, meta.album, meta.title)
    # for c in it_cands:
    #     if c.get("image_bytes"):
    #         # Prefer first viable
    #         # Normalize single-genre for writing (use iTunes primary if present)
    #         primary_genre = c.get("genre")
    #         return {
    #             **c,
    #             "genre": primary_genre,
    #             "genres": [primary_genre] if primary_genre else []
    #         }
    best = None
    # Prefer a candidate that has a track_number (usually "song" entity)
    for c in it_cands:
        if c.get("image_bytes") and c.get("track_number"):
            best = c
            break
    # Fallback: any candidate with an image
    if not best:
        for c in it_cands:
            if c.get("image_bytes"):
                best = c
                break

    if best:
        primary_genre = best.get("genre")
        return {
            **best,
            "genre": primary_genre,
            "genres": [primary_genre] if primary_genre else []
        }


    # MusicBrainz paths
    mb = None
    album_title_from_mb = None
    mb = mb_find_release_by_album_artist(meta.artist, meta.album)
    if not mb and (meta.artist and meta.title):
        mb = mb_find_release_by_artist_title(meta.artist, meta.title)
    if mb:
        mbid, album_title_from_mb = mb
        details = mb_fetch_release_details(mbid)
        caa = caa_fetch_front(mbid)
        if caa:
            img_bytes, ct = caa
            genres = details.get("genres") or []
            return {
                "image_bytes": img_bytes,
                "content_type": ct,
                "source": "CoverArtArchive",
                "album_title": album_title_from_mb,
                "release_date": details.get("release_date"),
                "genre": (genres[0] if genres else None),
                "genres": genres,
                "artist_name": meta.artist,
                "track_title": meta.title
            }

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
    year_set: Optional[bool] = None
    year_value: Optional[str] = None
    genre_set: Optional[bool] = None
    genre_value: Optional[str] = None
    artist_set: Optional[bool] = None
    artist_value: Optional[str] = None
    title_set: Optional[bool] = None
    title_value: Optional[str] = None
    track_set: Optional[bool] = None
    track_value: Optional[str] = None


def process_file(path: Path, args) -> WorkResult:
    try:
        if path.suffix.lower() != ".mp3":
            return WorkResult(path, "skip", detail="not mp3")

        # We will still attempt tag updates even if art exists when not forcing
        already_has_art = has_embedded_art(path)

        # Extract minimal meta; support "Artist - Title" filename heuristic
        meta = read_id3_meta(path)
        if not (meta.artist or meta.title or meta.album):
            m = re.match(r"(.+?)\s*-\s*(.+)$", path.stem)
            if m:
                meta = TrackMeta(artist=m.group(1).strip(), album=None, title=m.group(2).strip())

        found = find_cover_and_details(meta)

        album_set_flag = year_set_flag = genre_set_flag = artist_set_flag = title_set_flag = None
        album_value = year_value = genre_value = artist_value = title_value = None

        track_set_flag = None
        track_value = None

        if found:
            album_value = found.get("album_title")
            year_value = found.get("release_date")
            genre_value = found.get("genre")
            artist_value = found.get("artist_name")
            title_value = found.get("track_title")
            track_no = found.get("track_number")
            track_ct = found.get("track_count")
            if track_no:
                track_value = str(track_no)
                if track_ct:
                    track_value = f"{track_no}/{track_ct}"
                else:
                    track_value = str(track_no)

            if album_value and (args.update_album or not meta.album):
                album_set_flag = write_album_tag(path, album_value, force=args.force)

            if year_value:
                if args.update_year:
                    year_set_flag = write_year_tag(path, year_value, force=args.force)
                else:
                    # write only if missing when not explicitly requested
                    year_set_flag = write_year_tag(path, year_value, force=False)

            if genre_value and args.update_genre:
                genre_set_flag = write_genre_tag(path, genre_value, force=args.force)

            if artist_value and args.update_artist:
                artist_set_flag = write_artist_tag(path, artist_value, force=args.force)

            if title_value and args.update_title:
                title_set_flag = write_title_tag(path, title_value, force=args.force)
            
            if track_value and args.update_track:
                track_set_flag = write_track_tag(path, track_value, force=args.force)

        if args.dry_run:
            if not found:
                # Still report attempted tag writes
                return WorkResult(path, "miss", detail="no cover/details found",
                                  album_set=False, year_set=False, genre_set=False, artist_set=False, title_set=False)
            img_bytes = found.get("image_bytes") or b""
            return WorkResult(
                path, "found",
                source=found.get("source"),
                bytes_written=len(img_bytes),
                album_set=bool(album_set_flag) if album_set_flag is not None else False,
                album_value=album_value,
                year_set=bool(year_set_flag) if year_set_flag is not None else False,
                year_value=year_value,
                genre_set=bool(genre_set_flag) if genre_set_flag is not None else False,
                genre_value=genre_value,
                artist_set=bool(artist_set_flag) if artist_set_flag is not None else False,
                artist_value=artist_value,
                title_set=bool(title_set_flag) if title_set_flag is not None else False,
                title_value=title_value,
                track_set=track_set_flag,
                track_value=track_value

            )

        # If we already have art and not forcing, maybe skip embedding
        if found:
            img_bytes = found.get("image_bytes")
            mime = found.get("content_type") or "image/jpeg"
            if already_has_art and not args.force:
                status = "skip"
                detail = "already has art"
            else:
                if img_bytes:
                    ok = embed_cover(path, img_bytes, mime, force_id3v24=args.id3v24)
                    status = "ok" if ok else "error"
                    detail = None if ok else "embed failed"
                else:
                    status = "ok"  # tags possibly updated even without image
                    detail = "no image to embed"
            return WorkResult(
                path, status, source=found.get("source"), detail=detail,
                bytes_written=(len(img_bytes) if img_bytes else 0),
                album_set=album_set_flag, album_value=album_value,
                year_set=year_set_flag, year_value=year_value,
                genre_set=genre_set_flag, genre_value=genre_value,
                artist_set=artist_set_flag, artist_value=artist_value,
                title_set=title_set_flag, title_value=title_value,
                track_set=track_set_flag,
                track_value=track_value

            )
        else:
            return WorkResult(path, "miss", detail="no cover/details found",
                              album_set=False, year_set=False, genre_set=False, artist_set=False, title_set=False)
    except Exception as e:
        return WorkResult(path, "error", detail=str(e))

def iter_mp3s(root: Path, recursive: bool):
    if recursive:
        yield from (p for p in root.rglob("*.mp3") if p.is_file())
    else:
        yield from (p for p in root.glob("*.mp3") if p.is_file())

def main():
    parser = argparse.ArgumentParser(description="Find & embed cover art; optionally set Album/Year/Genre/Artist/Title tags.")
    parser.add_argument("-p", "--path", type=str, required=True, help="Folder containing MP3 files")
    parser.add_argument("-r", "--recursive", action="store_true", help="Scan subfolders")
    parser.add_argument("-n", "--concurrency", type=int, default=4, help="Parallel workers (not used in this simplified runner)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing cover art and (with --update-*) tags")
    parser.add_argument("--dry-run", action="store_true", help="Search & report only; do not embed")
    parser.add_argument("--id3v24", action="store_true", help="Save tags as ID3v2.4 (default v2.3)")

    # Tag update controls
    parser.add_argument("--update-album", action="store_true", help="Write album tag when discovered (default writes only if missing)")
    parser.add_argument("--update-year", action="store_true", help="Write year/date (TDRC) when discovered")
    parser.add_argument("--update-genre", action="store_true", help="Write genre (TCON) when discovered")
    parser.add_argument("--update-artist", action="store_true", help="Write artist (TPE1) when discovered")
    parser.add_argument("--update-title", action="store_true", help="Write title (TIT2) when discovered")
    parser.add_argument("--update-track", action="store_true", help="Write track number (TRCK) when discovered")


    args = parser.parse_args()

    root = Path(args.path).expanduser()
    if not root.exists():
        print(f"[!] Path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    files = list(iter_mp3s(root, args.recursive))
    if not files:
        print("[i] No MP3 files found.")
        return

    print(f"[i] Processing {len(files)} file(s) in {root} (recursive={args.recursive}) "
          f"dry_run={args.dry_run} force={args.force} "
          f"update_album={args.update_album} update_year={args.update_year} "
          f"update_genre={args.update_genre} update_artist={args.update_artist} update_title={args.update_title}")

    ok = sk = miss = err = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(process_file, p, args): p for p in files}
        for fut in as_completed(futs):
            res = fut.result()
            if res.status == "ok":
                ok += 1
                extras = []
                if res.album_set is not None and res.album_value:
                    extras.append(f"album={'set' if res.album_set else 'kept'} ('{res.album_value}')")
                if res.year_set is not None and res.year_value:
                    extras.append(f"year={'set' if res.year_set else 'kept'} ('{res.year_value}')")
                if res.genre_set is not None and res.genre_value:
                    extras.append(f"genre={'set' if res.genre_set else 'kept'} ('{res.genre_value}')")
                if res.artist_set is not None and res.artist_value:
                    extras.append(f"artist={'set' if res.artist_set else 'kept'} ('{res.artist_value}')")
                if res.title_set is not None and res.title_value:
                    extras.append(f"title={'set' if res.title_set else 'kept'} ('{res.title_value}')")
                if res.track_set is not None and res.track_value:
                    extras.append(f"track={'set' if res.track_set else 'kept'} ('{res.track_value}')")

                extra = (", " + ", ".join(extras)) if extras else ""
                print(f"[OK] {res.path} ({res.source}, wrote {res.bytes_written or 0} bytes){extra}")
            elif res.status == "found":
                extras = []
                if res.album_value: extras.append(f"album would write '{res.album_value}'")
                if res.year_value: extras.append(f"year would write '{res.year_value}'")
                if res.genre_value: extras.append(f"genre would write '{res.genre_value}'")
                if res.artist_value: extras.append(f"artist would write '{res.artist_value}'")
                if res.title_value: extras.append(f"title would write '{res.title_value}'")
                extra = (", " + ", ".join(extras)) if extras else ""
                print(f"[FOUND] {res.path} ({res.source}, would embed {res.bytes_written or 0} bytes{extra})")
            elif res.status == "skip":
                sk += 1
                print(f"[SKIP] {res.path} ({res.detail})")
            elif res.status == "miss":
                miss += 1
                print(f"[MISS] {res.path} ({res.detail})")
            else:
                err += 1
                print(f"[ERR] {res.path} ({res.detail})")

    print(f"\n[i] Done. ok={ok} skip={sk} miss={miss} err={err} of {len(files)}")

if __name__ == "__main__":
    main()
