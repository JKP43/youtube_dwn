import os
from mutagen.mp3 import MP3
from mutagen.id3 import ID3

# === CONFIG ===
folder = r""  # ← Change this to your folder path

# === SCRIPT ===
for filename in os.listdir(folder):
    if filename.lower().endswith(".mp3"):
        filepath = os.path.join(folder, filename)
        try:
            audio = MP3(filepath, ID3=ID3)

            # Remove album name
            if 'TALB' in audio.tags:
                del audio.tags['TALB']

            # Remove embedded pictures
            for tag in list(audio.tags.keys()):
                if tag.startswith('APIC'):
                    del audio.tags[tag]

            audio.save()
            print(f"✅ Cleared: {filename}")

        except Exception as e:
            print(f"⚠️ Error processing {filename}: {e}")

print("🎵 All MP3s processed successfully!")
