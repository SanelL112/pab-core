import os
import json
import logging
import asyncio
from dotenv import load_dotenv
from scrapers.google_scraper import download_drive_file
import tempfile
import pypdf
import httpx

load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.getLogger("pypdf").setLevel(logging.ERROR)

import warnings
warnings.filterwarnings("ignore", module="pypdf")

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
        if not isinstance(item, dict) or 'title' not in item or 'file_id' not in item:
            logger.warning("Skipping untyped/invalid queue item, preserving it.")
            continue
            
        title = item['title']
        file_id = item['file_id']
        
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            path = tmp.name
            
        if download_drive_file(file_id, path):
            try:
                reader = pypdf.PdfReader(path)
                text = ""
                for page in reader.pages:
                    text += page.extract_text() + "\n"
                    
                if len(text.strip()) <= 50:
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
            
    # Write back only failed items and untyped items
    remaining = [item for item in queue if item not in successful]
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(queue_path), suffix='.tmp')
        with os.fdopen(fd, 'w', encoding="utf-8") as f:
            json.dump(remaining, f, indent=2)
        os.replace(tmp_path, queue_path)
    except Exception as e:
        logger.error(f"Failed to write back nightly queue: {e}")
    
if __name__ == "__main__":
    import sys
    # For testing manually
    from telegram import Bot
    from config import SANEL_CHAT_ID
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if TELEGRAM_BOT_TOKEN:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        asyncio.run(run_nightly_job(bot, SANEL_CHAT_ID))
