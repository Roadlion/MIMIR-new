# test_f5.py
import os
import sys
import ssl

# Disable SSL verification for huggingface_hub and urllib3/httpx
ssl._create_default_https_context = ssl._create_unverified_context
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["CURL_CA_BUNDLE"] = ""
os.environ["PYTHONHTTPSVERIFY"] = "0"
os.environ["SSL_CERT_FILE"] = ""

import urllib3
urllib3.disable_warnings()

import soundfile as sf
import torch
import torchaudio

def _patched_torchaudio_load(filepath, **kwargs):
    data, samplerate = sf.read(filepath)
    tensor = torch.from_numpy(data).float()
    if tensor.ndim == 1:
        tensor = tensor.unsqueeze(0)
    elif tensor.ndim == 2:
        tensor = tensor.t()
    return tensor, samplerate

torchaudio.load = _patched_torchaudio_load

import httpx
_orig_httpx_init = httpx.Client.__init__
def _patched_httpx_init(self, *args, **kwargs):
    kwargs['verify'] = False
    _orig_httpx_init(self, *args, **kwargs)
httpx.Client.__init__ = _patched_httpx_init

_orig_httpx_async_init = httpx.AsyncClient.__init__
def _patched_httpx_async_init(self, *args, **kwargs):
    kwargs['verify'] = False
    _orig_httpx_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_httpx_async_init

from f5_tts.infer.infer_cli import main as f5_main

def test_clone():
    sample_file = os.path.abspath("backend/app/assets/voice/mimir_sample.wav")
    output_dir = os.path.abspath("frontend/static/audio/cache")
    output_file = "test_f5_output.wav"
    
    print(f"Testing F5-TTS voice clone using sample WAV: {sample_file}")
    
    # Override sys.argv to simulate CLI args with SSL disabled
    sys.argv = [
        "f5_tts_infer",
        "-r", sample_file,
        "-t", "Well now, Laddie! S and P 500 is steady, but mind your portfolio risk today.",
        "-o", output_dir,
        "-w", output_file
    ]
    
    try:
        f5_main()
        out_path = os.path.join(output_dir, output_file)
        if os.path.exists(out_path):
            print(f"SUCCESS: F5-TTS Voice Clone generated audio file: {out_path} ({os.path.getsize(out_path)} bytes)")
        else:
            print("F5-TTS completed but output file not found.")
    except Exception as e:
        print(f"F5-TTS error: {e}")

if __name__ == "__main__":
    test_clone()
