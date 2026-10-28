import os
import re
import json
import glob
import html
import shutil
import asyncio
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Callable, Optional

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw

AGY_BIN = r"C:\Users\User\AppData\Local\agy\bin\agy.exe"
if not os.path.exists(AGY_BIN):
    AGY_BIN = shutil.which("agy") or "agy"

VIDEO_DOMAINS = (
    "youtube.com", "youtu.be",
    "facebook.com", "fb.watch", "fb.com",
    "tiktok.com",
    "instagram.com",
    "twitter.com", "x.com",
)


def extract_urls_from_text(text: str) -> List[str]:
    if not text:
        return []
    pattern = r"https?://[^\s<>\"']+"
    urls = re.findall(pattern, text)
    cleaned = []
    for u in urls:
        u = u.rstrip(".,;!) individual")
        if u not in cleaned:
            cleaned.append(u)
    return cleaned


def is_video_url(url: str) -> bool:
    u_low = url.lower()
    if any(d in u_low for d in VIDEO_DOMAINS):
        # Facebook photo posts or pure text posts might still be handled by yt-dlp or fallback to web scrape
        return True
    return False


def build_contact_sheets(frames_dir: Path, output_dir: Path, max_sheets: int = 4) -> List[str]:
    """Build timestamped 4x4 contact sheets from extracted 1fps frames."""
    frames = sorted(glob.glob(str(frames_dir / "frame_*.jpg")))
    if not frames:
        return []

    total = len(frames)
    # If clip is very long (e.g. > 64 seconds), sample up to 64 frames evenly across the whole timeline
    max_frames = max_sheets * 16
    if total > max_frames:
        step = total / max_frames
        selected = [frames[int(i * step)] for i in range(max_frames)]
    else:
        selected = frames

    sheet_paths = []
    for sheet_idx in range(0, len(selected), 16):
        chunk = selected[sheet_idx:sheet_idx + 16]
        if not chunk:
            continue
        imgs = [Image.open(p) for p in chunk]
        w, h = imgs[0].size
        rows = (len(chunk) + 3) // 4
        grid = Image.new("RGB", (w * 4, h * rows), (15, 23, 42))
        draw = ImageDraw.Draw(grid)
        for i, (im, fpath) in enumerate(zip(imgs, chunk)):
            gx, gy = (i % 4) * w, (i // 4) * h
            grid.paste(im, (gx, gy))
            m = re.search(r"frame_(\d+)\.jpg", os.path.basename(fpath))
            sec = int(m.group(1)) if m else (sheet_idx + i + 1)
            mins, secs = divmod(sec, 60)
            label = f"{mins}:{secs:02d} ({sec}s)"
            draw.rectangle([gx + 6, gy + 6, gx + 145, gy + 42], fill=(0, 0, 0))
            draw.text((gx + 14, gy + 12), label, fill=(255, 230, 0))
        out_name = f"sheet_{sheet_idx // 16 + 1}.jpg"
        out_path = output_dir / out_name
        grid.save(str(out_path), quality=88)
        sheet_paths.append(out_name)

    return sheet_paths


def scrape_webpage_text(url: str) -> Dict[str, Any]:
    """Fallback/article scraper for news websites or public posts."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()

        og_desc = ""
        meta_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            og_desc = meta_desc["content"].strip()

        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            tag.decompose()

        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all(["p", "article", "h1", "h2", "h3"])]
        paragraphs = [p for p in paragraphs if len(p) > 25]
        seen = set()
        unique_paras = []
        for p in paragraphs:
            if p not in seen:
                seen.add(p)
                unique_paras.append(p)

        body_text = "\n\n".join(unique_paras[:40])
        return {
            "url": url,
            "title": html.unescape(title),
            "description": html.unescape(og_desc),
            "body_text": html.unescape(body_text[:8000]),
        }
    except Exception as e:
        return {"url": url, "error": str(e)}


async def run_subprocess(cmd: List[str], cwd: Path, timeout: int = 180) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return await asyncio.to_thread(
        subprocess.run,
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


async def process_job(
    job_id: str,
    job_dir: Path,
    urls: List[str],
    raw_text: str,
    focus_instruction: str,
    output_mode: str,
    image_files: List[str],
    progress_cb: Callable[[str, int], None],
) -> Dict[str, Any]:
    """
    Full pipeline:
    1. Download video(s) via yt-dlp & extract metadata + audio + timestamped frame contact sheets.
    2. Scrape any article/webpage URLs.
    3. Invoke Antigravity CLI (agy.exe) on the prepared job folder to inspect frames, audio, images,
       and perform 4-Lock Fact Verification.
    """
    job_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    extracted_meta: Dict[str, Any] = {
        "job_id": job_id,
        "submitted_urls": urls,
        "employee_raw_text": raw_text,
        "employee_focus_instruction": focus_instruction,
        "output_mode": output_mode,
        "uploaded_images": image_files,
        "videos": [],
        "articles": [],
    }

    all_contact_sheets: List[str] = []
    has_audio_file = False

    # Step 1: Process URLs
    if urls:
        for idx, url in enumerate(urls):
            progress_cb(f"กำลังดึงข้อมูลจากลิงก์ ({idx + 1}/{len(urls)})...", 20)
            if is_video_url(url):
                # Try downloading with yt-dlp
                cmd = [
                    "yt-dlp",
                    "--write-info-json",
                    "--no-playlist",
                    "-o", "video.%(ext)s",
                    url,
                ]
                res = await run_subprocess(cmd, cwd=job_dir, timeout=120)

                info_path = job_dir / "video.info.json"
                video_path = job_dir / "video.mp4"
                if not video_path.exists():
                    # Check if another extension was created
                    v_candidates = glob.glob(str(job_dir / "video.*"))
                    v_candidates = [p for p in v_candidates if not p.endswith(".json")]
                    if v_candidates:
                        video_path = Path(v_candidates[0])

                if info_path.exists():
                    try:
                        with open(info_path, "r", encoding="utf-8") as f:
                            vinfo = json.load(f)
                        ts = vinfo.get("timestamp")
                        dt_str = (
                            datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                            if ts else vinfo.get("upload_date", "ไม่ระบุ")
                        )
                        extracted_meta["videos"].append({
                            "url": url,
                            "title": html.unescape(str(vinfo.get("title") or "")),
                            "description": html.unescape(str(vinfo.get("description") or "")),
                            "uploader": html.unescape(str(vinfo.get("uploader") or "")),
                            "duration_seconds": vinfo.get("duration"),
                            "upload_timestamp": dt_str,
                            "upload_date": vinfo.get("upload_date"),
                            "view_count": vinfo.get("view_count"),
                        })
                    except Exception as e:
                        extracted_meta["videos"].append({"url": url, "info_error": str(e)})

                if video_path.exists():
                    progress_cb("กำลังแยกไฟล์เสียงและแกะเฟรมภาพทุกวินาทีจากคลิปจริง (ffmpeg)...", 40)
                    # Extract compressed mp3 audio
                    audio_path = job_dir / "audio.mp3"
                    await run_subprocess(
                        ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "24000", "-b:a", "48k", str(audio_path)],
                        cwd=job_dir,
                        timeout=90,
                    )
                    if audio_path.exists() and audio_path.stat().st_size > 1000:
                        has_audio_file = True

                    # Extract 1fps frames
                    await run_subprocess(
                        ["ffmpeg", "-y", "-i", str(video_path), "-vf", "fps=1,scale=640:-1", str(frames_dir / "frame_%03d.jpg")],
                        cwd=job_dir,
                        timeout=90,
                    )
                    sheets = await asyncio.to_thread(build_contact_sheets, frames_dir, job_dir, 4)
                    all_contact_sheets.extend(sheets)
                else:
                    # Fallback to webpage scrape if yt-dlp couldn't find a video (e.g. Facebook text/photo share link)
                    article_data = await asyncio.to_thread(scrape_webpage_text, url)
                    extracted_meta["articles"].append(article_data)
            else:
                progress_cb(f"กำลังดึงเนื้อหาจากหน้าเว็บข่าว ({idx + 1}/{len(urls)})...", 35)
                article_data = await asyncio.to_thread(scrape_webpage_text, url)
                extracted_meta["articles"].append(article_data)

    # Save metadata summary into job_dir/job_context.json
    context_file = job_dir / "job_context.json"
    with open(context_file, "w", encoding="utf-8") as f:
        json.dump(extracted_meta, f, ensure_ascii=False, indent=2)

    progress_cb("AI กำลังดูภาพ-ฟังเสียงจริง และตรวจสอบข้อเท็จจริง 4 ชั้น (Antigravity)...", 65)

    # Build instructions for Antigravity Agent
    files_to_inspect = ["job_context.json"]
    for s in all_contact_sheets:
        files_to_inspect.append(s)
    if has_audio_file:
        files_to_inspect.append("audio.mp3")
    for img in image_files:
        files_to_inspect.append(img)

    files_list_str = ", ".join(f"`{fn}`" for fn in files_to_inspect)

    if output_mode == "strict_clip_only":
        mode_instruction = (
            "โหมด: ใช้เฉพาะข้อมูลที่ได้จากคลิป/ภาพ/ข้อความต้นทาง 100% เท่านั้น "
            "(ห้ามใช้ search_web ค้นหาข่าวภายนอกมาปนเด็ดขาด) สรุปเป็นเนื้อข่าวดิบเน้นข้อเท็จจริงล้วน"
        )
    elif output_mode == "viral_3_para":
        mode_instruction = (
            "โหมด: ค้นเชื่อมโยงบริบทต้นเรื่องภายใต้กฎ 4-Lock Verification แล้วเขียนเป็น "
            "'เนื้อข่าวไวรัล 3 ย่อหน้าทองคำ' (ย่อหน้า 1 ปูบารมี/จุดตัดอารมณ์, ย่อหน้า 2 ทันทีที่...สิ่งแรกที่ทำไม่ใช่...แต่คือ..., "
            "ย่อหน้า 3 สรุปเชิดชูคุณค่า) พร้อมแนบเนื้อข่าวดิบแยกไว้ในช่อง visual_audio_facts"
        )
    else:
        mode_instruction = (
            "โหมดมาตรฐาน: แกะภาพและเสียงจากคลิป/ภาพจริง 100% ร่วมกับค้นเชื่อมโยงบริบทต้นเรื่องและชื่อเต็มด้วย `search_web` "
            "ภายใต้กฎล็อกความแม่นยำ 4 ชั้น (4-Lock Verification) แล้วเขียนเป็น 'เนื้อข่าวดิบเน้นข้อเท็จจริงล้วน' "
            "ในรูปแบบที่มนุษย์เขียน อ่านเข้าใจลำดับเหตุการณ์จริงชัดเจน ไม่เติมแต่งอารมณ์ฟุ่มเฟือย และไม่ใช้สำนวนบอท"
        )

    focus_str = f"\nประเด็นที่พนักงานสั่งเน้นเป็นพิเศษ: {focus_instruction}\n" if focus_instruction else ""

    prompt = f"""คุณคือระบบวิเคราะห์และสรุปเนื้อข่าวความแม่นยำสูงจากคลิปและแหล่งข้อมูลจริง
ในโฟลเดอร์งาน `{job_dir.as_posix()}` มีไฟล์ที่ดึงมาจากต้นทางเรียบร้อยแล้ว ได้แก่: {files_list_str}

ขั้นตอนการทำงานบังคับ:
1. ใช้เครื่องมือ `read_file` เปิดอ่านไฟล์ทั้งหมดในรายการด้านบน:
   - อ่าน `job_context.json` เพื่อดูวันเวลาโพสต์จริง (upload_timestamp), แคปชัน, ชื่อผู้โพสต์ และข้อความจากพนักงาน
   - หากมีไฟล์ภาพ Contact Sheet (`sheet_1.jpg`, `sheet_2.jpg`, ...) หรือภาพที่พนักงานอัปโหลด (`uploaded_*`) ต้องเปิดดูด้วย `read_file` ทุกไฟล์ เพื่อดูเหตุการณ์ การกระทำ สีหน้า สถานที่ ป้ายข้อความในภาพ
   - **กรณีคลิปกล้องวงจรปิด (CCTV) / กล้องหน้ารถ / คลิปเหตุการณ์ที่ไม่มีเสียงบรรยาย:** ให้ใช้การอ่านภาพทีละเฟรมตามตัวเลขวินาทีสีเหลืองมุมซ้ายบนของแต่ละช่องภาพ เพื่อไล่ลำดับการกระทำเป็นฉากๆ อย่างละเอียด:
     (ก) สภาพแวดล้อมและจุดสังเกต (สถานที่, เลขเวลาบนกล้องวงจรปิดถ้ามี, ป้ายร้าน/ป้ายถนน, ลักษณะบุคคล/สีเสื้อผ้า, ยี่ห้อ/สีรถ)
     (ข) จังหวะเริ่มต้นก่อนเกิดเหตุ (ใครอยู่ตรงไหน กำลังทำอะไร)
     (ค) จังหวะเกิดเหตุสำคัญ/จุดพีค (ใครเคลื่อนไหวอย่างไร ทำท่าทางอะไร ใช้อะไรทำอะไร)
     (ง) จังหวะต่อเนื่องและผลลัพธ์ตอนจบคลิป (คนรอบข้างทำอย่างไร เหตุการณ์จบลงอย่างไร)
   - หากมีไฟล์เสียง `audio.mp3` ต้องเปิดฟังด้วย `read_file` เพื่อแกะคำพูดและบทสัมภาษณ์จริงในคลิป (หรือฟังเสียงสภาพแวดล้อม เช่น เสียงแตร เสียงชน เสียงตะโกน)
2. {mode_instruction}
{focus_str}
3. กฎล็อกความแม่นยำ 4 ชั้น (4-Lock Verification) เมื่อต้องค้นเชื่อมโยงด้วย `search_web`:
   - Lock 1 (Timestamp Lock): ยึดช่วงเวลาเดียวกับ `upload_timestamp` ในคลิป/โพสต์ ห้ามนำข่าวเก่าคนละปีมาปน
   - Lock 2 (Clip Genre Gate): ถ้าเป็นคลิปคนดัง/ข่าวใหญ่/ตัดจากรายการ ให้ค้นเชื่อมโยงต้นเรื่องและชื่อ-นามสกุลเต็ม แต่ถ้าเป็นคลิปชาวบ้านทั่วไป/กล้องวงจรปิดที่ไม่มีสำนักข่าวรายงานเคสเดียวกันเป๊ะๆ ห้ามนำข่าวคล้ายกันจากเคสอื่นมาสวมทับเด็ดขาด ให้ใช้เฉพาะสิ่งที่เห็นและได้ยินในคลิป 100%
   - Lock 3 (3-Point Match): ข้อมูลที่ค้นมาต้องตรงกับภาพและเสียงในคลิปครบ 3 จุด (บุคคลเดียวกัน + ชุด/สถานที่/บรรยากาศตรงกัน + ช่วงเวลาเดียวกัน)
   - Lock 4 (Audit Tag): ระบุที่มาและการตรวจสอบความแม่นยำให้ชัดเจน

เมื่อวิเคราะห์เสร็จแล้ว ให้บันทึกผลลัพธ์เป็นไฟล์ JSON ชื่อ `result.json` ลงในโฟลเดอร์ `{job_dir.as_posix()}/result.json` ด้วยเครื่องมือ `write_file` (และพิมพ์ JSON เดียวกันออกมาเป็นคำตอบสุดท้ายด้วย) โดยมีโครงสร้าง JSON ดังนี้เท่านั้น:
{{
  "headline": "หัวข้อสรุปประเด็นข่าวสั้นๆ เข้าใจทันที",
  "raw_news": "เนื้อข่าวดิบเน้นข้อเท็จจริงล้วน (3-4 ย่อหน้า เขียนแบบมนุษย์เล่าลำดับข้อเท็จจริง ใคร ทำอะไร ที่ไหน อย่างไร มีคำพูดสำคัญอะไรบ้าง ไม่ใช้คำแพตเทิร์นบอท)",
  "visual_audio_breakdown": "สรุปสิ่งที่เห็นจากภาพและคำพูดจริงที่ได้ยินจากเสียงในคลิป/โพสต์แบบละเอียด",
  "context_connected": "ข้อมูลบริบทต้นเรื่องหรือชื่อเต็มที่ค้นเชื่อมโยงเพิ่มเติม (หากไม่ได้ค้นเพิ่มให้ระบุว่า 'ใช้ข้อมูลจากต้นฉบับในคลิป/โพสต์ 100%')",
  "audit_tag": "✅ [ที่มาข้อมูล: ระบุชัดเจนว่าแกะจากภาพ-เสียงในคลิป 100% และ/หรือ เชื่อมโยงข่าวจริงวันที่เท่าไหร่ ตรงกันจุดไหนบ้าง]"
}}"""

    cmd_agy = [
        AGY_BIN,
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--add-dir", str(job_dir),
        "--print", prompt,
    ]

    agy_res = await run_subprocess(cmd_agy, cwd=job_dir, timeout=240)
    progress_cb("กำลังจัดรูปแบบผลลัพธ์และบันทึกข้อมูล...", 92)

    result_data: Optional[Dict[str, Any]] = None
    result_json_path = job_dir / "result.json"

    if result_json_path.exists():
        try:
            with open(result_json_path, "r", encoding="utf-8") as f:
                result_data = json.load(f)
        except Exception:
            result_data = None

    if not result_data:
        # Try parsing JSON from stdout
        stdout_text = agy_res.stdout or ""
        m = re.search(r"\{[\s\S]*\"raw_news\"[\s\S]*\}", stdout_text)
        if m:
            try:
                result_data = json.loads(m.group(0))
            except Exception:
                pass

    if not result_data:
        raw_out = (agy_res.stdout or "").strip() or (agy_res.stderr or "ไม่สามารถประมวลผลได้")
        result_data = {
            "headline": "สรุปเนื้อข่าวจากต้นฉบับ",
            "raw_news": raw_out,
            "visual_audio_breakdown": "ดูรายละเอียดในเนื้อข่าวด้านบน",
            "context_connected": "-",
            "audit_tag": "✅ [ที่มาข้อมูล: ประมวลผลผ่าน Antigravity Agent บนเครื่องแม่]",
        }

    result_data["contact_sheets"] = [f"/jobs/{job_id}/{s}" for s in all_contact_sheets]
    result_data["uploaded_images"] = [f"/jobs/{job_id}/{img}" for img in image_files]
    result_data["metadata"] = extracted_meta

    progress_cb("เสร็จสมบูรณ์ พร้อมใช้งาน!", 100)
    return result_data
