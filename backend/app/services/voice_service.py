# backend/app/services/voice_service.py
import os
import sys
import hashlib
import asyncio
import logging
import urllib3
urllib3.disable_warnings()

# Monkeypatch SSL for httpx and torchaudio for soundfile to ensure smooth F5-TTS voice cloning
try:
    import httpx
    _orig_httpx_init = httpx.Client.__init__
    def _patched_httpx_init(self, *args, **kwargs):
        kwargs['verify'] = False
        _orig_httpx_init(self, *args, **kwargs)
    httpx.Client.__init__ = _patched_httpx_init
except Exception:
    pass

# Removed unnecessary torchaudio import that was causing Windows DLL popups

logger = logging.getLogger(__name__)

# Base directory paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
VOICE_ASSETS_DIR = os.path.join(BASE_DIR, "backend", "app", "assets", "voice")
CACHE_DIR = os.path.join(BASE_DIR, "frontend", "static", "audio", "cache")

os.makedirs(VOICE_ASSETS_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

def get_custom_voice_sample_path() -> str | None:
    """Check if user has placed a custom Mimir audio sample (.wav or .mp3)."""
    wav_path = os.path.join(VOICE_ASSETS_DIR, "mimir_sample.wav")
    mp3_path = os.path.join(VOICE_ASSETS_DIR, "mimir_sample.mp3")
    
    # Prefer mp3 since user stated they use an mp3
    if os.path.exists(mp3_path):
        return mp3_path
    if os.path.exists(wav_path):
        return wav_path
        
    return None

async def generate_mimir_speech(text: str, voice_name: str = "en-GB-RyanNeural") -> str:
    """
    Synthesizes speech text into an audio file in the static audio cache.
    If mimir_sample is present, attempts F5-TTS zero-shot voice cloning.
    Falls back to edge-tts Scottish neural voice for sub-second response time.
    """
    if not text or not text.strip():
        text = "Aye, Brother. I have no market updates to report at this moment."
        
    text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
    custom_sample = get_custom_voice_sample_path()
    
    if custom_sample:
        filename = f"mimir_clone_{text_hash[:12]}.mp3"
        filepath = os.path.join(CACHE_DIR, filename)
        relative_url = f"/static/audio/cache/{filename}"

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            logger.info(f"Using cached cloned Mimir voice file: {filepath}")
            return relative_url

        # F5-TTS voice cloning via CUDA GPU (RTX 3060)
        logger.info(f"Cloning voice from sample '{custom_sample}' using F5-TTS CUDA GPU...")
        conda_python = r"E:\conda\python.exe"
        python_exe = conda_python if os.path.exists(conda_python) else sys.executable
        gpu_script = os.path.join(BASE_DIR, "run_gpu_clone.py")
        
        cmd = [
            python_exe, gpu_script,
            "-t", text,
            "-w", filepath,
            "-r", custom_sample
        ]
        
        env = os.environ.copy()
        env["PYTHONHTTPSVERIFY"] = "0"
        env["CURL_CA_BUNDLE"] = ""
        env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        env["PYTHONWARNINGS"] = "ignore"
        
        import subprocess
        try:
            def run_sync():
                return subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    timeout=90.0,
                    text=True,
                    errors="replace"
                )
            
            proc = await asyncio.to_thread(run_sync)
            
            with open("gpu_clone_debug.log", "w", encoding="utf-8") as f:
                f.write(f"RETURNCODE: {proc.returncode}\n")
                f.write(f"STDOUT:\n{proc.stdout}\n")
                f.write(f"STDERR:\n{proc.stderr}\n")

            if proc.returncode == 0 and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                logger.info(f"Successfully generated cloned Mimir speech: {filepath}")
                return relative_url
            else:
                logger.warning(f"F5-TTS CLI warning (returncode {proc.returncode}). Falling back to edge-tts.")
        except subprocess.TimeoutExpired:
            with open("gpu_clone_debug.log", "a", encoding="utf-8") as f:
                f.write("TIMEOUT EXCEEDED\n")
            logger.warning("F5-TTS voice cloning timed out on CPU. Falling back to instant edge-tts.")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            with open("gpu_clone_debug.log", "a", encoding="utf-8") as f:
                f.write(f"EXCEPTION: {repr(e)}\nTRACEBACK:\n{tb}\n")
            logger.error(f"F5-TTS execution error: {repr(e)}. Falling back to edge-tts.")

    # Instant Fallback: Edge-TTS Scottish Neural voice (<0.5s response time)
    filename = f"mimir_recap_{text_hash[:12]}.mp3"
    filepath = os.path.join(CACHE_DIR, filename)
    relative_url = f"/static/audio/cache/{filename}"

    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        logger.info(f"Using cached Edge-TTS audio file: {filepath}")
        return relative_url

    try:
        import edge_tts
        import ssl
        import certifi
        
        try:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ssl_ctx = ssl._create_unverified_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        
        if hasattr(edge_tts, "communicate"):
            edge_tts.communicate._SSL_CTX = ssl_ctx

        communicate = edge_tts.Communicate(
            text, 
            voice=voice_name,
            pitch="-4Hz",
            rate="-3%"
        )
        await communicate.save(filepath)
        logger.info(f"Generated new Mimir Edge-TTS audio file: {filepath}")
        return relative_url
    except Exception as e:
        logger.error(f"Error in speech generation: {str(e)}")
        raise e
