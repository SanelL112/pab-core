import asyncio
import os
import sys
import datetime
import subprocess
import shlex
from functools import wraps

# Ensure the parent directory is in sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from scrapers.mega_study_builder import generate_mega_guide
from llm_router import call_openrouter
from utils import scrub_pii, retry
from config import CACHE_DIR


def run_cmd(cmd: str, timeout: int = 120, cwd: str = None) -> subprocess.CompletedProcess:
    """Run a command with proper timeout and error handling."""
    args = shlex.split(cmd)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        check=False,  # We'll check returncode manually
    )


def run_cmd_safe(cmd: str, timeout: int = 120, cwd: str = None) -> tuple[bool, str]:
    """Run a command safely, returning (success, output)."""
    try:
        result = run_cmd(cmd, timeout=timeout, cwd=cwd)
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, f"Exit code {result.returncode}: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"
    except Exception as e:
        return False, f"Error: {e}"


def extract_dynamic_topics(pdf_text: str, num_topics: int = 3) -> list:
    """Uses the AI to extract new, highly specific topics from the user's classroom notes."""
    prompt = f"""You are an intelligent topic extractor. Scan the following classroom notes and identify exactly {num_topics} distinct, highly specific academic topics or chapters that would make great study guides.
Respond ONLY with a comma-separated list of the topics. Do not include bullet points or numbers.
Example: Quadratic Equations, Civil War History, Biology Cell Structure

Notes:
{pdf_text[:15000]}"""
    print("Extracting dynamic topics from classroom notes using OpenRouter...")

    scrubbed_prompt = scrub_pii(prompt)
    
    # Try local RPC first (no rate limits)
    from llm_router import call_local_rpc
    result = call_local_rpc(
        prompt=scrubbed_prompt,
        max_tokens=500,
        timeout=180,
        classification="PRIVATE",
    )
    if not result or result.startswith("⚠️"):
        print("Topic extraction: local RPC failed or unavailable.")
        result = None

    if result:
        topics = [t.strip() for t in result.split(",") if t.strip()]
        if topics:
            return topics[:num_topics]

    return ["General Mathematics", "Advanced Grammar", "Test Strategies"]


def build_and_push(topic: str):
    """Generates or updates the massive study guide and automatically pushes it to GitHub."""
    print(f"\n==============================================")
    print(f"Starting Nightly Build Pipeline for: {topic}")
    print(f"==============================================\n")

    filename_base = topic.replace(" ", "_").replace("/", "_")
    output_md = f"/home/sanel/personal-assistant-bot/study_guides/{filename_base}_Study_Guide.md"
    output_docx = f"/home/sanel/personal-assistant-bot/study_guides/{filename_base}_Study_Guide.docx"

    # Check if we should append to save tokens instead of rebuilding
    if os.path.exists(output_md):
        print(f"Study guide for {topic} already exists. Running lightweight append-only update to save tokens...")

        internal_notes = ""
        notes_file = os.path.join(CACHE_DIR, "combined_summaries.txt")
        pdf_file = os.path.join(CACHE_DIR, "pdf_exports.txt")

        if os.path.exists(notes_file):
            with open(notes_file, "r") as f:
                internal_notes += f.read().replace('\x00', '').strip()
        if os.path.exists(pdf_file):
            with open(pdf_file, "r") as f:
                internal_notes += "\n\n" + f.read().replace('\x00', '').strip()

        if not internal_notes.strip():
            print("No new classroom notes to append. Skipping update.")
            return

        today = datetime.datetime.now().strftime("%Y-%m-%d")
        prompt = f"""You are an elite academic tutor. We are appending a new section to an existing master study guide on "{topic}".
Below are the LATEST classroom notes from today.
Write a highly detailed new section titled "## 📅 Update: {today} - New Concepts" that perfectly synthesizes these new notes.
DO NOT rewrite the entire study guide, ONLY output the new section to be appended to the bottom.

--- NEW CLASSROOM NOTES ---
{internal_notes}"""
        print("Calling OpenRouter for Delta-Append generation...")

        scrubbed_prompt = scrub_pii(prompt)
        new_section = ""
        try:
            from llm_router import call_local_rpc
            new_section = call_local_rpc(
                prompt=scrubbed_prompt,
                max_tokens=4000,
                timeout=300,
                classification="PRIVATE",
            )
        except Exception as e:
            print(f"Local RPC error: {e}")

        if not new_section or new_section.startswith("⚠️"):
            print("Local RPC append failed or unavailable.")
            new_section = None

        if new_section:
            # Clean up <thought> tags
            import re
            new_section = re.sub(r'<thought>.*?</thought>', '', new_section, flags=re.DOTALL).strip()

            with open(output_md, "a", encoding="utf-8") as f:
                f.write(f"\n\n---\n\n{new_section}\n")
            print(f"Successfully appended new concepts to {output_md}")
            result = True
        else:
            print("Failed to generate append section.")
            result = False

    else:
        print(f"No existing guide found for {topic}. Running full 10-chapter Mega Build...")
        result_text = generate_mega_guide(topic)
        if result_text:
            with open(output_md, "w", encoding="utf-8") as f:
                f.write(result_text)
            print(f"Successfully created study guide at {output_md}")
            result = True
        else:
            result = False

    if result:
        # 3. Convert to DOCX format
        print("Converting Markdown to DOCX format...")
        success, output = run_cmd_safe(f"pandoc {shlex.quote(output_md)} -o {shlex.quote(output_docx)}", timeout=60)
        if not success:
            print(f"Pandoc failed: {output}")
            return
        print(f"Successfully created Word document at {output_docx}")
    else:
        print(f"Failed to process study guide for {topic}.")


def main():
    print(f"=== Nightly Processor Started at {datetime.datetime.now()} ===")

    # Ensure the script is running in the correct directory for git tracking
    os.chdir("/home/sanel/personal-assistant-bot")

    # 0. Sync New Google Classroom & Google Drive Files
    print("Executing Google Classroom & Drive Sync...")
    try:
        from scrapers.nightly_processor import run_nightly_job
        from config import SANEL_CHAT_ID
        # Safely run the async sync job in the event loop
        asyncio.run(run_nightly_job(None, SANEL_CHAT_ID))
        print("Successfully synced and downloaded all new queued PDFs and Docs into the memory bank.")
    except Exception as e:
        print(f"Warning: Failed to drain nightly sync queue: {e}")

    # 1. Separated Core Study Guides (Disciplinary Split)
    build_and_push("SAT Math and Geometry Master Guide")
    build_and_push("SAT Reading Comprehension Master Guide")
    build_and_push("SAT Writing and Grammar Master Guide")

    # 2. Load the Google Classroom PDF text to find dynamic topics
    pdf_path = os.path.join(CACHE_DIR, "pdf_exports.txt")
    pdf_text = ""
    if os.path.exists(pdf_path):
        with open(pdf_path, "r", encoding="utf-8", errors="ignore") as f:
            pdf_text = f.read()
            # Strip null-bytes just to be safe
            pdf_text = pdf_text.replace('\x00', '')

    if pdf_text:
        # 3. Extract 3 dynamic topics based on the classroom material
        topics = extract_dynamic_topics(pdf_text, num_topics=3)
        print(f"Discovered dynamic topics for tonight's run: {topics}")

        # 4. Build a massive study guide for each discovered topic
        # SAFETY LIMIT: max 3 dynamic topics per night (already enforced by extract_dynamic_topics)
        max_topics = 3
        for dynamic_topic in topics[:max_topics]:
            build_and_push(dynamic_topic)
    else:
        print("No Classroom PDF data found. Skipping dynamic topic generation.")

    print(f"=== Nightly Processor Finished at {datetime.datetime.now()} ===")


if __name__ == "__main__":
    import time

    # Run the main processor
    main()
