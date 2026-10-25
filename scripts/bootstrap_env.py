# scripts/bootstrap_env.py
"""
MIMIR Zero-Touch Environment Bootstrapper
Automatically ensures .env exists and installs any missing Python dependencies
into the active virtual environment so MIMIR runs out-of-the-box on any PC.
Uses only Python standard library modules so it can run in a bare venv.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_PACKAGES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("psycopg2", "psycopg2-binary"),
    ("dotenv", "python-dotenv"),
    ("requests", "requests"),
    ("urllib3", "urllib3"),
    ("certifi", "certifi"),
    ("redis", "redis"),
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("sklearn", "scikit-learn"),
    ("jinja2", "jinja2"),
    ("yfinance", "yfinance"),
    ("curl_cffi", "curl_cffi"),
    ("bs4", "beautifulsoup4"),
    ("lxml", "lxml"),
    ("psutil", "psutil"),
    ("docx", "python-docx"),
    ("feedparser", "feedparser"),
    ("edge_tts", "edge-tts"),
]

if sys.platform == "win32":
    REQUIRED_PACKAGES.append(("MetaTrader5", "MetaTrader5"))


def ensure_env_file():
    env_path = PROJECT_ROOT / ".env"
    example_path = PROJECT_ROOT / ".env.example"
    if not env_path.exists():
        if example_path.exists():
            shutil.copyfile(example_path, env_path)
            print(" [BOOTSTRAP] Created default .env from .env.example.")
        else:
            env_path.write_text(
                "DB_HOST=localhost\nDB_PORT=5432\nDB_NAME=pantheon_db\n"
                "DB_USER=postgres\nDB_PASSWORD=\nMIMIR_SCHEMA=yggdrasil\n"
                "MIMIR_MODE=standalone\n",
                encoding="utf-8",
            )
            print(" [BOOTSTRAP] Created default .env configuration file.")


def ensure_dependencies():
    missing = []
    for mod_name, pkg_name in REQUIRED_PACKAGES:
        if importlib.util.find_spec(mod_name) is None:
            missing.append(pkg_name)

    if not missing:
        return True

    print("===================================================")
    print(f" [BOOTSTRAP] Missing {len(missing)} package(s): {', '.join(missing)}")
    print(" [BOOTSTRAP] Auto-installing dependencies now...")
    print("===================================================")

    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"] + missing
    res = subprocess.run(cmd)
    if res.returncode != 0:
        print(" [BOOTSTRAP WARNING] Some packages failed to install in batch. Trying one by one...")
        for pkg in missing:
            subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", pkg])

    return True


def main():
    ensure_env_file()
    ensure_dependencies()


if __name__ == "__main__":
    main()
