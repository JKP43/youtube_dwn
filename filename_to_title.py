import os
from pathlib import Path

# PDF
from PyPDF2 import PdfReader, PdfWriter

# DOCX
from docx import Document

# MP3 (ID3 tags)
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, TIT2
from mutagen.mp3 import MP3

def set_pdf_title(file_path, title):
    reader = PdfReader(file_path)
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    metadata = reader.metadata or {}
    metadata.update({"/Title": title})
    writer.add_metadata(metadata)
    with open(file_path, "wb") as f:
        writer.write(f)
    print(f"[PDF] Title updated → {title}")

def set_docx_title(file_path, title):
    doc = Document(file_path)
    props = doc.core_properties
    props.title = title
    doc.save(file_path)
    print(f"[DOCX] Title updated → {title}")

def set_mp3_title(file_path, title):
    audio = MP3(file_path, ID3=ID3)
    try:
        audio.add_tags()
    except Exception:
        pass
    audio["TIT2"] = TIT2(encoding=3, text=title)
    audio.save()
    print(f"[MP3] Title updated → {title}")

def main(folder):
    supported_exts = {".pdf", ".docx", ".mp3"}
    for file_path in Path(folder).glob("*"):
        if file_path.suffix.lower() not in supported_exts:
            continue
        title = file_path.stem
        try:
            if file_path.suffix.lower() == ".pdf":
                set_pdf_title(file_path, title)
            elif file_path.suffix.lower() == ".docx":
                set_docx_title(file_path, title)
            elif file_path.suffix.lower() == ".mp3":
                set_mp3_title(file_path, title)
        except Exception as e:
            print(f"⚠️ Error updating {file_path.name}: {e}")

if __name__ == "__main__":
    folder_to_process = input("Enter folder path: ").strip('"')
    if not os.path.isdir(folder_to_process):
        print("❌ Folder not found.")
    else:
        main(folder_to_process)
