import os
import re
import time
import shutil
import threading
import subprocess
from pathlib import Path
import uvicorn

BASE_DIR = Path(__file__).parent.resolve()
TUNNEL_FILE = BASE_DIR / "tunnel_url.txt"

CLOUDFLARED_PATHS = [
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    shutil.which("cloudflared") or "",
]


def start_cloudflare_tunnel(port: int = 8080):
    cf_bin = None
    for p in CLOUDFLARED_PATHS:
        if p and os.path.exists(p):
            cf_bin = p
            break
    if not cf_bin:
        print("[Tunnel] cloudflared not found; running on LAN/localhost only.")
        return

    try:
        proc = subprocess.Popen(
            [cf_bin, "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in proc.stdout:
            m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if m:
                url = m.group(0)
                TUNNEL_FILE.write_text(url, encoding="utf-8")
                print(f"\n=======================================================")
                print(f"🌐 ลิงก์ออนไลน์สำหรับส่งให้พนักงาน 10 คน: {url}")
                print(f"=======================================================\n")
    except Exception as e:
        print(f"[Tunnel Error] {e}")


if __name__ == "__main__":
    if TUNNEL_FILE.exists():
        try:
            TUNNEL_FILE.unlink()
        except Exception:
            pass

    t = threading.Thread(target=start_cloudflare_tunnel, args=(8080,), daemon=True)
    t.start()

    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=False)
