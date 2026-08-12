import os
import logging
import subprocess

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AGENTAPI_BIN = os.getenv("AGENTAPI_BIN", "/home/sanel/.local/bin/agy")


def _agy_model(alias: str) -> str:
    """Resolve an internal agy alias (flash/pro) to the current valid model ID."""
    try:
        import sys
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from llm_router import _resolve_agy_model
        return _resolve_agy_model(alias)
    except Exception:
        return "Gemini 3.1 Pro (Low)" if alias == "pro" else "Gemini 3.5 Flash (Medium)"


def transcribe_handwritten_pdf(pdf_path: str) -> str:
    """Optionally use an explicitly approved cloud vision transcription service.

    Uploaded school documents are private by default.  The caller must opt in
    at process start with ``PAB_ALLOW_CLOUD_DOCUMENT_TRANSCRIPTION=1``; the
    CLI is never given unattended host-tool permissions.
    """
    if os.getenv("PAB_ALLOW_CLOUD_DOCUMENT_TRANSCRIPTION", "").strip() != "1":
        return (
            "Error: Cloud document transcription is disabled for private files. "
            "Use the local OCR pipeline or explicitly enable the approved service."
        )

    logger.info(f"Asking Antigravity CLI to transcribe {pdf_path}...")
    
    prompt = (
        f"You are a multimodal AI vision tool. Use your view_file tool to read the PDF document at {pdf_path}. "
        "It contains handwritten mathematical and conceptual notes. Transcribe the handwriting exactly as it is written. "
        "Ignore doodles or illegible scribbles, but preserve all the core educational content and math formulas. "
        "Output ONLY the transcribed text."
    )
    
    try:
        result = subprocess.run(
            [AGENTAPI_BIN, "--model", _agy_model("pro"), "--print", prompt],
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode != 0:
            logger.error(f"agy CLI failed: {result.stderr}")
            return f"Error transcribing PDF: {result.stderr}"
            
        # Clean up any ANSI color codes from agy output
        import re
        clean = re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', result.stdout)
        clean = clean.replace('\r\n', '\n').replace('\r', '\n').strip()
        
        if not clean:
             return "Error: Antigravity CLI returned empty transcription."
             
        return clean
    except subprocess.TimeoutExpired:
        logger.error("Antigravity CLI timed out reading the PDF.")
        return "Error: Timed out while transcribing the PDF."
    except Exception as e:
        logger.error(f"Failed to transcribe PDF via agy: {e}")
        return f"Error transcribing PDF: {e}"

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(transcribe_handwritten_pdf(sys.argv[1]))
