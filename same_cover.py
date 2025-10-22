import os, io
from PIL import Image
from mutagen.id3 import ID3, APIC, ID3NoHeaderError

# === CONFIG ===
FOLDER = r""  # folder with MP3s (recurses)
COVER  = r""  # your picture

# --- Normalize the cover to a baseline RGB JPEG under ~1000px ---
def normalize_cover_to_jpeg_bytes(path, max_side=1000, quality=85):
    img = Image.open(path)
    img = img.convert("RGB")  # ensure RGB, not CMYK/Palette
    w, h = img.size
    scale = min(1.0, max_side / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    # baseline JPEG (no progressive) for compatibility
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=False)
    return buf.getvalue()

cover_bytes = normalize_cover_to_jpeg_bytes(COVER)
MIME = "image/jpeg"  # we normalized to JPEG

# --- Walk and embed into all MP3s ---
for root, _, files in os.walk(FOLDER):
    for name in files:
        if not name.lower().endswith(".mp3"):
            continue
        path = os.path.join(root, name)
        try:
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()

            # Remove any existing images (ID3v2.3 APIC and older PIC)
            tags.delall("APIC")
            tags.delall("PIC")

            # Add new front cover
            tags.add(APIC(
                encoding=3,    # UTF-8
                mime=MIME,     # "image/jpeg"
                type=3,        # 3 = front cover
                desc="Cover",
                data=cover_bytes
            ))

            # Save explicitly as ID3v2.3
            tags.save(path, v2_version=3)

            # Verify: reload and count APIC frames
            v = ID3(path)
            apics = [k for k in v.keys() if k.startswith("APIC")]
            print(f"✅ {path} — set 1 cover (APIC frames now: {len(apics)})")

        except Exception as e:
            print(f"⚠️ {path}: {e}")

print("\n🎵 Done. If you still don't see art, see the tips below.")
