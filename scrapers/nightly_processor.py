import os
import json
import logging
import asyncio
from dotenv import load_dotenv
from scrapers.google_scraper import download_drive_file
import tempfile
import PyPDF2
import httpx
from pathlib import Path

load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

async def run_nightly_job(bot, chat_id):
    from config import NIGHTLY_QUEUE_FILE, CACHE_DIR
    queue_path = NIGHTLY_QUEUE_FILE
    if not os.path.exists(queue_path):
        return
        
    with open(queue_path, "r") as f:
        queue = json.load(f)
        
    if not queue:
        return
        
    logger.info(f"Running nightly processing on {len(queue)} files...")
    
    successful = []
    
    for item in queue:
        title = item['title']
        file_id = item['file_id']
        source = item.get("source", "google_drive")
        filename = str(item.get("filename") or title)
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".docx", ".txt"}:
            suffix = ".pdf"  # Legacy Google queue items have no filename.

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            path = tmp.name

        if source == "canvas":
            from scrapers.canvas_scraper import download_canvas_file
            downloaded = download_canvas_file(file_id, path)
        else:
            downloaded = download_drive_file(file_id, path)

        if downloaded:
            try:
                if suffix == ".pdf":
                    reader = PyPDF2.PdfReader(path)
                    text = ""
                    for page in reader.pages:
                        text += page.extract_text() + "\n"
                elif suffix == ".docx":
                    from docx import Document
                    document = Document(path)
                    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
                else:
                    text = Path(path).read_text(encoding="utf-8", errors="replace")

                if suffix == ".pdf" and len(text.strip()) <= 50:
                    try:
                        import pytesseract
                        from pdf2image import convert_from_path
                        logger.info(f"Running nightly OCR on {title}...")
                        images = convert_from_path(path)
                        text = ""
                        for img in images:
                            text += pytesseract.image_to_string(img) + "\n"
                    except Exception as e:
                        logger.error(f"Failed OCR on {title}: {e}")
                
                if len(text.strip()) > 50:
                    # Export the extracted PDF text to the massive memory bank instead of sending a Telegram message
                    output_file = os.path.join(CACHE_DIR, "pdf_exports.txt")
                    os.makedirs(os.path.dirname(output_file), exist_ok=True)
                    
                    with open(output_file, "a", encoding="utf-8") as f:
                        f.write(f"\n\n=== EXPORTED PDF: {title} ===\n")
                        f.write(text)
                        
                    logger.info(f"Successfully exported {title} to text file.")
                    successful.append(item)
            except Exception as e:
                item["attempt_count"] = item.get("attempt_count", 0) + 1
                item["last_error"] = str(e)
                item["retryable"] = True
        else:
            item["attempt_count"] = item.get("attempt_count", 0) + 1
            item["last_error"] = "Download failed"
            item["retryable"] = True
                
        try:
            os.remove(path)
        except Exception:
            pass
            
    # Write back only failed items
    remaining = [item for item in queue if item not in successful]
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(queue_path), suffix='.tmp')
    with os.fdopen(fd, 'w', encoding="utf-8") as f:
        json.dump(remaining, f, indent=2)
    os.replace(tmp_path, queue_path)
    
if __name__ == "__main__":
    import sys
    # For testing manually
    from telegram import Bot
    from config import SANEL_CHAT_ID
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if TELEGRAM_BOT_TOKEN:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        asyncio.run(run_nightly_job(bot, SANEL_CHAT_ID))
