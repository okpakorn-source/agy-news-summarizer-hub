import os
import sys
import socket
import uvicorn

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

VERCEL_PUBLIC_URL = "https://agy-news-summarizer-hub.vercel.app"


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    lan_ip = get_lan_ip()
    print("\n" + "=" * 68)
    print("  🚀 Antigravity News Summarizer Hub — เครื่องแม่พร้อมรับงานจากพนักงาน!")
    print("=" * 68)
    print(f"  🌐 ลิงก์เว็บถาวรส่งให้พนักงาน : {VERCEL_PUBLIC_URL}")
    print(f"  📶 ลิงก์ Wi-Fi ในออฟฟิศ     : http://{lan_ip}:8080")
    print(f"  💻 หน้าจอควบคุมบนเครื่องนี้   : http://localhost:8080")
    print("-" * 68)
    print("  ✅ ระบบกำลังดึงงานจากเว็บพนักงานมารันในคอมเครื่องนี้อัตโนมัติทุก 3 วินาที")
    print("  (เปิดหน้าต่างนี้ทิ้งไว้ได้เลย หากต้องการปิดระบบให้กด Ctrl+C หรือปิดหน้าต่าง)")
    print("=" * 68 + "\n", flush=True)

    port = 8080
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])

    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
