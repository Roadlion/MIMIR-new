# run_server.py
"""
MIMIR Web Server & Cloudflare Tunnel Launcher

Starts the MIMIR FastAPI application on http://localhost:8000
and attempts to launch a Cloudflare Tunnel for secure remote access.
"""

import os
import sys
import subprocess
import shutil
import time

def check_cloudflared():
    return shutil.which("cloudflared") is not None

def print_banner():
    banner = """
  ███╗   ███╗██╗███╗   ███╗██╗██████╗ 
  ████╗ ████║██║████╗ ████║██║██╔══██╗
  ██╔████╔██║██║██╔████╔██║██║██████╔╝
  ██║╚██╔╝██║██║██║╚██╔╝██║██║██╔══██╗
  ██║ ╚═╝ ██║██║██║ ╚═╝ ██║██║██║  ██║
  ╚═╝     ╚═╝╚═╝╚═╝     ╚═╝╚═╝╚═╝  ╚═╝
  -- Remote Multi-User Hosting Engine --
    """
    print(banner)

def main():
    print_banner()
    
    python_exe = sys.executable
    cloudflared_path = shutil.which("cloudflared")

    print("[MIMIR] Local Server URL:  http://localhost:8000")
    print("[MIMIR] Default Admin Logins:")
    print("        Username: admin")
    print("        Password: admin123  (Change immediately in /admin dashboard)")

    print("\n[MIMIR] Starting FastAPI Server on port 8000...")
    uvicorn_cmd = [
        python_exe, "-m", "uvicorn", "backend.app.main:app",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--reload"
    ]
    
    server_process = subprocess.Popen(uvicorn_cmd)

    if cloudflared_path:
        print("\n[MIMIR] Cloudflare Tunnel detected! Launching public HTTPS tunnel...")
        time.sleep(2)
        tunnel_cmd = [cloudflared_path, "tunnel", "--url", "http://localhost:8000"]
        try:
            tunnel_process = subprocess.Popen(tunnel_cmd)
            tunnel_process.wait()
        except KeyboardInterrupt:
            print("\n[MIMIR] Shutting down Cloudflare Tunnel...")
    else:
        print("\n" + "="*70)
        print("  NOTICE: Cloudflare Tunnel ('cloudflared') is not currently installed.")
        print("  To enable HTTPS remote access for your friends anywhere in the world:")
        print("  1. Download cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-local-tunnel/")
        print("  2. Or run in terminal: winget install Cloudflare.cloudflared")
        print("  3. Run: cloudflared tunnel --url http://localhost:8000")
        print("="*70 + "\n")
        
        try:
            server_process.wait()
        except KeyboardInterrupt:
            print("\n[MIMIR] Server shut down.")

if __name__ == "__main__":
    main()
