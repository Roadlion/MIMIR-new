# run_gpu_clone.py
import os
import sys
import ssl
import ctypes

# Suppress Windows DLL error popups (Entry Point Not Found, etc.)
if os.name == 'nt':
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)

ssl._create_default_https_context = ssl._create_unverified_context
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["CURL_CA_BUNDLE"] = ""
os.environ["PYTHONHTTPSVERIFY"] = "0"

import urllib3
urllib3.disable_warnings()

import httpx
_orig_httpx_init = httpx.Client.__init__
def _patched_httpx_init(self, *args, **kwargs):
    kwargs['verify'] = False
    _orig_httpx_init(self, *args, **kwargs)
httpx.Client.__init__ = _patched_httpx_init

import soundfile as sf
import torch
print(f"CUDA Available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"GPU Device: {torch.cuda.get_device_name(0)}")

# Bypass torchaudio C++ extension load error on Windows Conda
try:
    import torchaudio._extension.utils
    torchaudio._extension.utils._load_lib = lambda name: None
except Exception:
    pass

import types
ext_mod = types.ModuleType("torchaudio._extension")
ext_mod._IS_TORCHAUDIO_EXT_AVAILABLE = False
ext_mod.fail_if_no_align = lambda fn: fn
ext_utils = types.ModuleType("torchaudio._extension.utils")
ext_utils._load_lib = lambda x: None
sys.modules["torchaudio._extension"] = ext_mod
sys.modules["torchaudio._extension.utils"] = ext_utils

import torchaudio
def _patched_load(filepath, **kwargs):
    data, sr = sf.read(filepath)
    t = torch.from_numpy(data).float()
    if t.ndim == 1: t = t.unsqueeze(0)
    elif t.ndim == 2: t = t.t()
    return t, sr
torchaudio.load = _patched_load

from f5_tts.model import DiT
from f5_tts.infer.utils_infer import load_model, load_vocoder, infer_process
from huggingface_hub import hf_hub_download

_CACHED_MODEL = None
_CACHED_VOCODER = None

def get_f5_models(device="cuda"):
    global _CACHED_MODEL, _CACHED_VOCODER
    if _CACHED_MODEL is None:
        print("Loading F5-TTS model into GPU VRAM...")
        model_cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
        ckpt_path = hf_hub_download(repo_id="SWivid/F5-TTS", filename="F5TTS_v1_Base/model_1250000.safetensors")
        _CACHED_MODEL = load_model(DiT, model_cfg, ckpt_path, device=device)
        _CACHED_VOCODER = load_vocoder(vocoder_name="vocos", device=device)
    return _CACHED_MODEL, _CACHED_VOCODER

def synthesize_gpu(text: str, output_filepath: str, ref_audio_path: str):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, vocoder = get_f5_models(device=device)
    
    actual_ref_audio = ref_audio_path
    if ref_audio_path.lower().endswith('.mp3'):
        try:
            from pydub import AudioSegment
            print(f"Converting MP3 reference audio to WAV...")
            sound = AudioSegment.from_file(ref_audio_path)
            temp_ref_wav = ref_audio_path + "_temp.wav"
            sound.export(temp_ref_wav, format="wav")
            actual_ref_audio = temp_ref_wav
        except Exception as e:
            print(f"Warning: Failed to convert MP3 reference to WAV: {e}")

    print(f"Synthesizing voice clone on {device.upper()}...")
    audio, sr, _ = infer_process(
        ref_audio=actual_ref_audio,
        ref_text="I always thought you were one of the dumbest creatures I'd ever met. Didn't expect you to be the bravest, too.",
        gen_text=text,
        model_obj=model,
        vocoder=vocoder,
        device=device
    )
    
    if actual_ref_audio != ref_audio_path and os.path.exists(actual_ref_audio):
        try:
            os.remove(actual_ref_audio)
        except:
            pass
    temp_wav = output_filepath if output_filepath.endswith(".wav") else output_filepath + ".wav"
    sf.write(temp_wav, audio, sr)
    
    if output_filepath.endswith(".mp3"):
        try:
            from pydub import AudioSegment
            sound = AudioSegment.from_file(temp_wav)
            sound.export(output_filepath, format="mp3", bitrate="128k")
            if os.path.exists(temp_wav) and temp_wav != output_filepath:
                os.remove(temp_wav)
        except Exception as e:
            print(f"Warning: MP3 conversion failed ({e}), keeping WAV file.")
            if os.path.exists(temp_wav) and temp_wav != output_filepath:
                os.rename(temp_wav, output_filepath)

    print(f"SUCCESS: GPU Voice Clone saved to {output_filepath} ({os.path.getsize(output_filepath)} bytes)")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--text", type=str, default="Aye, Brother! S and P 500 is steady today. Mind your portfolio risk.")
    parser.add_argument("-w", "--output", type=str, default=None)
    parser.add_argument("-r", "--ref", type=str, default=None)
    args = parser.parse_args()

    sample_file = args.ref if args.ref else os.path.abspath("backend/app/assets/voice/mimir_sample.wav")
    output_dir = os.path.abspath("frontend/static/audio/cache")
    output_filepath = args.output if args.output else os.path.join(output_dir, "mimir_gpu_clone_test.wav")
    
    synthesize_gpu(args.text, output_filepath, sample_file)
