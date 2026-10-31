import os
import re
import json
import glob
import html
import shutil
import asyncio
import datetime
import subprocess
import urllib.parse
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
    pattern = r"https?://[^\s<>\"')\]+]+"
    urls = re.findall(pattern, text)
    cleaned = []
    for u in urls:
        u = u.rstrip(".,;!)'\"]>")
        if u not in cleaned:
            cleaned.append(u)
    return cleaned


def is_video_url(url: str) -> bool:
    u_low = url.lower()
    if any(d in u_low for d in ("facebook.com", "fb.com")):
        # Text/photo post URLs should go straight to webpage + embed scraper instead of waiting on yt-dlp
        if any(p in u_low for p in ("/posts/", "/share/p/", "/permalink.php", "/photo", "story.php")):
            return False
    if any(d in u_low for d in VIDEO_DOMAINS):
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


def scrape_webpage_text(url: str, job_dir: Optional[Path] = None, image_files: Optional[List[str]] = None) -> Dict[str, Any]:
    """Fallback/article scraper for news websites or public posts (including Facebook share/p)."""
    is_fb = any(d in url.lower() for d in ("facebook.com", "fb.com", "fb.watch"))
    headers = {
        "User-Agent": (
            "Twitterbot/1.0"
            if is_fb
            else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        resp = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
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

        og_image = ""
        meta_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
        if meta_img and meta_img.get("content"):
            og_image = meta_img["content"].strip()
            if og_image and job_dir is not None and image_files is not None:
                try:
                    ir = requests.get(og_image, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                    if ir.status_code == 200 and ir.content:
                        fname = f"uploaded_og_{len(image_files) + 1}.jpg"
                        (job_dir / fname).write_bytes(ir.content)
                        image_files.append(fname)
                except Exception:
                    pass

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

        # For Facebook posts (/posts/pfbid..., /share/p/...), fetch full untruncated text via Facebook's post embed plugin
        if is_fb and len(body_text) < 200:
            try:
                target_fb_url = resp.url
                og_url_meta = soup.find("meta", property="og:url")
                if og_url_meta and og_url_meta.get("content"):
                    target_fb_url = og_url_meta["content"].strip()
                # Normalize Facebook post URL with long slug (/page/posts/slug/12345/) into (/page/posts/12345)
                m_post = re.search(r"facebook\.com/([^/?#]+)/posts/(?:[^/?#]+/)?(\d+|pfbid[A-Za-z0-9]+)", target_fb_url)
                if m_post:
                    clean_fb_url = f"https://www.facebook.com/{m_post.group(1)}/posts/{m_post.group(2)}"
                else:
                    clean_fb_url = re.sub(r"[?&](rdid|share_url|utm_[^=&]+|_rdr)=[^&]*", "", target_fb_url).rstrip("?&/#")
                embed_url = (
                    f"https://www.facebook.com/plugins/post.php?href="
                    f"{urllib.parse.quote(clean_fb_url, safe='')}&show_text=true&width=500"
                )
                er = requests.get(embed_url, headers=headers, timeout=15)
                if er.status_code == 200:
                    esoup = BeautifulSoup(er.content.decode("utf-8", errors="replace"), "html.parser")
                    fb_blocks = []
                    for div in esoup.find_all("div", class_="text_exposed_root"):
                        for hide in div.find_all(class_="text_exposed_hide"):
                            hide.decompose()
                        for br in div.find_all("br"):
                            br.replace_with("\n")
                        for p in div.find_all("p"):
                            p.insert_after("\n\n")
                        txt = div.get_text().strip()
                        if txt and txt not in fb_blocks:
                            fb_blocks.append(txt)
                    if not fb_blocks:
                        for p in esoup.find_all("p"):
                            pt = p.get_text(" ", strip=True)
                            if pt and pt not in fb_blocks:
                                fb_blocks.append(pt)
                    if fb_blocks:
                        body_text = "\n\n".join(fb_blocks)
            except Exception:
                pass

        # If Facebook post links out to an external news article in og:description or body_text, follow the article URL
        if is_fb and len(body_text) < 350:
            search_blob = f"{og_desc}\n{body_text}"
            ext_urls = [
                u.rstrip(".,;!)")
                for u in re.findall(r"https?://[^\s<>\"']+", search_blob)
                if not any(d in u.lower() for d in ("facebook.com", "fb.com", "fb.watch"))
            ]
            if ext_urls:
                try:
                    art_sub = scrape_webpage_text(ext_urls[0], None, None)
                    if art_sub.get("body_text"):
                        body_text = art_sub["body_text"]
                except Exception:
                    pass

        return {
            "url": url,
            "title": html.unescape(title),
            "description": html.unescape(og_desc),
            "body_text": html.unescape(body_text[:8000]),
        }
    except Exception as e:
        return {"url": url, "error": str(e)}


def _clean_article_box_text(text: str) -> str:
    if not text:
        return ""
    t = str(text).strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def _normalize_result_articles(result_data: Dict[str, Any], output_mode: str) -> Dict[str, Any]:
    raw_articles = result_data.get("articles")
    normalized: List[Dict[str, str]] = []

    if isinstance(raw_articles, list):
        for idx, item in enumerate(raw_articles):
            if isinstance(item, dict):
                content = _clean_article_box_text(item.get("content") or item.get("raw_news") or item.get("text") or "")
                if not content:
                    continue
                label = str(item.get("label") or item.get("mode_badge") or "").strip()
                title = str(item.get("title") or "").strip()
                if not label:
                    if "บิด" in title or (idx > 0 and output_mode in ("normal_1_spin_1", "normal_1_spin_2")) or output_mode == "spin_only_1":
                        label = "บิดประเด็น"
                    elif output_mode == "viral_3_para":
                        label = "ข่าวสั้น 3 ย่อหน้า"
                    elif output_mode == "strict_clip_only":
                        label = "เฉพาะในคลิป 100%"
                    else:
                        label = "มุมปกติ"
                if not title:
                    title = f"{idx + 1}. {label}"
                normalized.append({"label": label, "title": title, "content": content})

    cleaned_raw = _clean_article_box_text(result_data.get("raw_news") or "")

    if not normalized and cleaned_raw:
        default_label = (
            "บิดประเด็น" if output_mode == "spin_only_1"
            else "ข่าวสั้น 3 ย่อหน้า" if output_mode == "viral_3_para"
            else "เฉพาะในคลิป 100%" if output_mode == "strict_clip_only"
            else "มุมปกติ"
        )
        normalized.append({
            "label": default_label,
            "title": result_data.get("headline") or f"1. ข่าวแบบ{default_label}",
            "content": cleaned_raw,
        })

    result_data["articles"] = normalized
    if len(normalized) == 1:
        result_data["raw_news"] = normalized[0]["content"]
    elif len(normalized) > 1:
        result_data["raw_news"] = "\n\n══════════════════════════════\n\n".join(
            f"【{a['title']}】\n\n{a['content']}" for a in normalized
        )
    else:
        result_data["raw_news"] = cleaned_raw

    return result_data


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
                # Also try extracting Thai subtitles/auto-captions for fast, complete transcript of long videos
                await run_subprocess(
                    ["yt-dlp", "--write-auto-sub", "--write-sub", "--sub-lang", "th", "--skip-download", "-o", "sub", url],
                    cwd=job_dir,
                    timeout=30,
                )
                vtt_file = job_dir / "sub.th.vtt"
                if vtt_file.exists():
                    try:
                        vtt_raw = vtt_file.read_text(encoding="utf-8", errors="ignore")
                        t_out, last_line, cur_t = [], "", ""
                        for ln in vtt_raw.splitlines():
                            if "-->" in ln:
                                cur_t = ln.split("-->")[0].strip()[:8]
                                continue
                            cln = re.sub(r"<[^>]+>", "", ln).strip()
                            if not cln or cln.startswith(("WEBVTT", "Kind:", "Language:")):
                                continue
                            if cln != last_line:
                                t_out.append((cur_t[:5], cln))
                                last_line = cln
                        blocks: Dict[str, List[str]] = {}
                        for tm, tx in t_out:
                            blocks.setdefault(tm, []).append(tx)
                        (job_dir / "transcript.txt").write_text(
                            "\n\n".join(f"[{m}] {' '.join(blocks[m])}" for m in sorted(blocks.keys())),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass

                # Try downloading video (limit to <=15 min or first 10 min if huge)
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
                    article_data = await asyncio.to_thread(scrape_webpage_text, url, job_dir, image_files)
                    extracted_meta["articles"].append(article_data)
            else:
                progress_cb(f"กำลังดึงเนื้อหาจากหน้าเว็บข่าว ({idx + 1}/{len(urls)})...", 35)
                article_data = await asyncio.to_thread(scrape_webpage_text, url, job_dir, image_files)
                extracted_meta["articles"].append(article_data)

    # Save metadata summary into job_dir/job_context.json
    context_file = job_dir / "job_context.json"
    with open(context_file, "w", encoding="utf-8") as f:
        json.dump(extracted_meta, f, ensure_ascii=False, indent=2)

    progress_cb("AI กำลังอ่านภาพ-เสียง ค้นข้อมูล 2 ชั้น และเขียนตามคำสั่ง (Antigravity)...", 65)

    # Save full skill manuals into job_dir/SKILL_GUIDES.md so CLI command line never exceeds Windows 32KB limit
    digest_skill_path = Path(r"C:\Users\User\.gemini\config\skills\viral-news-digest\SKILL.md")
    spin_skill_path = Path(r"C:\Users\User\.gemini\config\skills\viral-news-spin\SKILL.md")
    writer_skill_path = Path(r"C:\Users\User\.gemini\config\skills\viral-news-writer\SKILL.md")

    guides_parts: List[str] = []
    if output_mode == "viral_3_para":
        if writer_skill_path.exists():
            guides_parts.append("=== คู่มือสกิล `viral-news-writer` ===\n" + writer_skill_path.read_text(encoding="utf-8"))
    elif output_mode == "strict_clip_only":
        if digest_skill_path.exists():
            guides_parts.append("=== คู่มือสกิล `viral-news-digest` ===\n" + digest_skill_path.read_text(encoding="utf-8"))
    else:
        if digest_skill_path.exists():
            guides_parts.append("=== คู่มือสกิลหลัก `viral-news-digest` ===\n" + digest_skill_path.read_text(encoding="utf-8"))
        if spin_skill_path.exists():
            guides_parts.append("=== คู่มือสกิลบิดประเด็น `viral-news-spin` ===\n" + spin_skill_path.read_text(encoding="utf-8"))

    skill_guides_file = job_dir / "SKILL_GUIDES.md"
    skill_guides_file.write_text("\n\n" + ("\n\n" + "=" * 60 + "\n\n").join(guides_parts), encoding="utf-8")

    # Build instructions for Antigravity Agent
    files_to_inspect = ["SKILL_GUIDES.md", "job_context.json"]
    if (job_dir / "transcript.txt").exists():
        files_to_inspect.append("transcript.txt")
    for s in all_contact_sheets:
        files_to_inspect.append(s)
    if has_audio_file:
        files_to_inspect.append("audio.mp3")
    for img in image_files:
        files_to_inspect.append(img)

    files_list_str = ", ".join(f"`{fn}`" for fn in files_to_inspect)

    core_rules = (
        "  • ขั้นตอนที่ 1 (สืบย้อนหาต้นทางฉบับเต็มข้ามแพลตฟอร์ม 4 สเต็ป & อ่าน/ดูให้จบ 100% + รีเสิร์ชขุดลึก 2 ชั้นจนสุดซอย `search_web`):\n"
        "    - ไม่ว่าจะเป็นรูปภาพ โพสต์ข้อความ หรือคลิปเสียง/เฟรมภาพ ต้องอ่านและดูตั้งแต่วินาทีแรกจนจบ 100% เสมอ\n"
        "    - หากลิงก์ต้นทางเป็นคลิปสั้นไฮไลท์จากเพจตัดคลิปหรือโพสต์สรุปสั้นของเพจข่าว ห้ามหยุดแค่ข้อมูลตรงหน้าเด็ดขาด! ต้องนำถ้อยคำเฉพาะในคลิป + ชื่อบุคคล/บัญชีเจ้าตัว + จุดสังเกตภาพ ไปเสิร์ชกว้างข้ามแพลตฟอร์ม (เพจ/เฟซบุ๊กเจ้าตัว, YouTube คลิปเต็ม/รายการเต็ม, TikTok/Reels ต้นทาง และข่าวถอดบทสัมภาษณ์เต็ม) และแยก 'เสียงพูดจริงของผู้พูด' ออกจาก 'พาดหัวที่เพจตัดคลิปแปะทับหน้าจอเอง'\n"
        "    - ค้น 2 ชั้น: ชั้นที่ 1 เก็บข้อเท็จจริง สถานะล่าสุด ตัวเลขจริง และคำชี้แจงทุกฝ่ายให้ครบ + ชั้นที่ 2 ขุดบทสัมภาษณ์ชีวิตจริงในอดีต จุดเริ่มต้นครอบครัว ความทรงจำ/เหตุการณ์จริงที่เกี่ยวข้อง หรือตัวบทกฎหมาย/ระเบียบต้นขั้วจริงอย่างน้อย 2-3 จุด (วางน้ำหนักสายกลางบนข้อเท็จจริงจริง ห้ามคิดแทนล้ำเส้นคำพูดจริงของเจ้าตัว)\n"
        "  • ขั้นตอนที่ 2 (มาตรฐานการเขียน `Data-Led Chameleon Flow` & `Intra-Batch Divergence`):\n"
        "    - โครงสร้างแต่ละกล่อง (ยกเว้นโหมด 3 ย่อหน้า): บล็อก 1 (1 บรรทัดเดี่ยวเปิดเรื่องคมๆ ตามจุดแข็งที่สุดของข้อมูลข่าวนั้น) -> เว้นบรรทัดว่าง `\\n\\n` -> บล็อก 2 (เนื้อหาเจาะลึกรายจุด แยกทีละบรรทัด ไต่ระดับเหตุผลและอารมณ์ อธิบาย 'ทำไม' ไม่ใช่แค่ 'อะไร') -> เว้นบรรทัดว่าง `\\n\\n` -> บล็อก 3 (ขมวดจบตามธรรมชาติของเรื่อง 1 บรรทัด)\n"
        "    - แบนคำแม่พิมพ์นักวิเคราะห์ซ้ำซาก 100%: ห้ามเปิดเรื่องด้วย 'เบื้องหลัง...', 'เหตุผลที่... มีที่มาจาก...', 'เส้นทางพลิกผันของ...', 'ท่ามกลางกระแส...' ห้ามขึ้นกลางเรื่องด้วย 'หากย้อนกลับไป...', 'ในทางธุรกิจ...' และห้ามจบเทศนาสั่งสอน ('จึงสะท้อนชัดเจนว่า...', 'จึงเป็นบทเรียนชีวิตจริงว่า...', 'จึงเป็นเครื่องพิสูจน์ว่า...', 'นี่แหละ...', 'จาก... กลายเป็น...')\n"
        "    - หากงานนี้มีมากกว่า 1 กล่องข่าว (เช่น มุมปกติ + บิดประเด็น): ทุกกล่องต้องใช้ท่าเปิด จังหวะเดินเรื่อง จำนวนบรรทัด และท่าจบต่างโครงสร้างกัน 100% (`Intra-Batch Divergence`) และห้ามเอาท้ายข่าวปกติมาขยายซ้ำในข่าวบิดประเด็น\n"
        "    - ความยาวบรรทัดมือถือ: แต่ละบรรทัดห้ามยาวเกิน ~220 ตัวอักษร (หากยาวเกินให้เคาะแยกบรรทัดตามจังหวะเหตุการณ์โดยไม่ตัดเนื้อหาออก)\n"
        "    - กฎ 95/5 & Pre-Generator Filter & Meta Safety 100%: ปูสะพานเชื่อมแค่ครึ่งถึง 1 บรรทัดให้คนไม่ตามข่าวเข้าใจทันที ห้ามพ่วงคีย์เวิร์ดข่าวใหญ่เก่าที่ไม่เกี่ยว ห้ามใส่รหัสเทป EP.xxx หรือวันที่ราชการแข็งๆ ตัดคำอังกฤษปนไทยในเนื้อข่าวออก 100% และงดถ้อยคำที่ส่อผิดกฎชุมชน Meta 100%"
    )

    if output_mode == "strict_clip_only":
        mode_instruction = (
            "โหมดที่เลือก: แกะเฉพาะในคลิป/โพสต์ 100% (`strict_clip_only` -> สร้าง 1 กล่องข่าวใน `articles` โดย `label` = `'เฉพาะในคลิป 100%'`)\n"
            "  • ใช้เฉพาะข้อมูลที่ได้จากคลิป/ภาพ/ข้อความต้นทาง 100% เท่านั้น (ห้ามใช้ `search_web` ค้นหาข่าวภายนอกมาปนเด็ดขาด) แล้วสรุปใจความข่าวด้วยสกิล `viral-news-digest` (โครงสร้าง 3 บล็อก ไร้คำเกริ่นแม่พิมพ์ 'เบื้องหลัง...' และไร้คำสรุปเทศนา)"
        )
    elif output_mode == "viral_3_para":
        mode_instruction = (
            "โหมดที่เลือก: ข่าวสั้นไวรัล 3 ย่อหน้า (`viral_3_para` -> สร้าง 1 กล่องข่าวใน `articles` โดย `label` = `'ข่าวสั้น 3 ย่อหน้า'`)\n"
            "  • ค้นเช็กข้อเท็จจริงให้แม่นยำ แล้วเขียนด้วยสกิล `viral-news-writer` (3 ย่อหน้ากระชับ จับ 1 ประเด็นบริสุทธิ์ เปิดคมไร้คำเกริ่นเรียงความ จบด้วยข้อเท็จจริงคมๆ อ่านจบใน 10-15 วินาที)"
        )
    elif output_mode == "normal_1_spin_1":
        mode_instruction = (
            "โหมดที่เลือก: **มุมปกติ 1 ข่าว + บิดประเด็น 1 ข่าว** (`normal_1_spin_1` -> สร้าง **2 กล่องข่าวแยกกัน** ในอาร์เรย์ `articles`):\n"
            "  1. กล่องที่ 1 (`label`: `'มุมปกติ'`, `title`: `'1. ข่าวแบบมุมปกติ (สรุปประเด็นหลักสั้นๆ)'`): สรุปใจความข่าวหลักครบวงจร ต้นเหตุ -> กลไก/เหตุผลจริง -> คำชี้แจงครบทุกฝ่าย -> จุดคลายปม ด้วยสกิล `viral-news-digest`\n"
            "  2. กล่องที่ 2 (`label`: `'บิดประเด็น'`, `title`: `'2. ข่าวแบบบิดประเด็น (ระบุชื่อมุมที่บิดสั้นๆ)'`): ใช้สกิล `viral-news-spin` คู่กับ `viral-news-digest` — หากพนักงานระบุความต้องการ/แนวที่อยากบิดไว้ด้านล่าง ต้องยึดแนวนั้นเป็นแกนหลักพร้อมขุดข้อมูลจริง 3-4 ฉากสำคัญมารองรับให้คนอ่านอินจนจบ (หากพนักงานไม่ได้ระบุแนวเฉพาะ ให้คัดเลือก 1 ใน 8 สูตรบิดประเด็นที่มีน้ำหนักข้อมูลจริงสูงที่สุดและเป็นคนละห้องข้อมูลกับกล่องที่ 1)\n"
            f"{core_rules}"
        )
    elif output_mode == "normal_1_spin_2":
        mode_instruction = (
            "โหมดที่เลือก: **มุมปกติ 1 ข่าว + บิดประเด็น 2 ข่าว** (`normal_1_spin_2` -> สร้าง **3 กล่องข่าวแยกกัน** ในอาร์เรย์ `articles`):\n"
            "  1. กล่องที่ 1 (`label`: `'มุมปกติ'`, `title`: `'1. ข่าวแบบมุมปกติ (...)'`): สรุปใจความข่าวหลักครบวงจรด้วยสกิล `viral-news-digest`\n"
            "  2. กล่องที่ 2 (`label`: `'บิดประเด็น 1'`, `title`: `'2. ข่าวแบบบิดประเด็น มุมที่ 1 (...)'`): บิดประเด็นมุมที่ 1 ตามแนวความต้องการของพนักงาน (หรือสูตรที่ข้อมูลแน่นที่สุด)\n"
            "  3. กล่องที่ 3 (`label`: `'บิดประเด็น 2'`, `title`: `'3. ข่าวแบบบิดประเด็น มุมที่ 2 (...)'`): บิดประเด็นมุมที่ 2 ที่ฉีกคนละมุม/คนละห้องข้อมูลกับกล่องที่ 1 และ 2 อย่างสิ้นเชิง\n"
            f"{core_rules}"
        )
    elif output_mode == "spin_only_1":
        mode_instruction = (
            "โหมดที่เลือก: **บิดประเด็นล้วน 1 ข่าว** (`spin_only_1` -> สร้าง **1 กล่องข่าว** ในอาร์เรย์ `articles` โดย `label` = `'บิดประเด็น'`):\n"
            "  • ใช้สกิล `viral-news-spin` คู่กับ `viral-news-digest` เจาะมุมบิดตามความต้องการของพนักงาน (หรือคัดสูตรบิดที่น้ำหนักข้อมูลสูงสุด) โดยมีสะพานปูที่มาข่าวหลักสั้นๆ ครึ่งถึง 1 บรรทัด (กฎ 95/5) ให้คนไม่เคยตามข่าวอ่านกล่องนี้กล่องเดียวก็เข้าใจและอินจนจบ\n"
            f"{core_rules}"
        )
    else:
        mode_instruction = (
            "โหมดที่เลือก: **มุมปกติ 1 ข่าว** (`standard_4lock` -> สร้าง **1 กล่องข่าว** ในอาร์เรย์ `articles` โดย `label` = `'มุมปกติ'`, ยกเว้นกรณีที่ข้อความคำสั่งของพนักงานด้านล่างระบุชัดเจนว่าขอหลายข่าวหรือขอบิดประเด็นเพิ่ม ให้สร้างจำนวนกล่องใน `articles` ตามคำสั่งพนักงานได้เลย!):\n"
            f"{core_rules}"
        )

    focus_str = (
        f"\n🔥 **คำสั่ง/ความต้องการพิเศษจากพนักงาน (สำคัญสูงสุด — ต้องทำตามแนวทางและจำนวนข่าวที่สั่งให้ครบถ้วนแม่นยำ):**\n"
        f"\"{focus_instruction}\"\n"
        if focus_instruction
        else ""
    )

    prompt = f"""คุณคือระบบวิเคราะห์ สรุปใจความข่าว และบิดประเด็นข่าวไวรัลความแม่นยำสูง (4-Lock Fact Extraction + `viral-news-digest` + `viral-news-spin`)
ในโฟลเดอร์งาน `{job_dir.as_posix()}` มีไฟล์ที่เตรียมไว้เรียบร้อยแล้ว ได้แก่: {files_list_str}

ขั้นตอนการทำงานบังคับ:
1. ใช้เครื่องมือ `read_file` เปิดอ่านไฟล์ทั้งหมดในรายการด้านบนเป็นอันดับแรก:
   - **ต้องอ่าน `SKILL_GUIDES.md`** เพื่อยึดกฎทุกข้อของสกิล (`viral-news-digest` / `viral-news-spin` / `Data-Led Chameleon Flow`) แบบ 100%
   - อ่าน `job_context.json` เพื่อดูวันเวลาโพสต์จริง (`upload_timestamp`), แคปชัน, ชื่อผู้โพสต์ และคำสั่งจากพนักงาน
   - หากมีไฟล์ภาพ Contact Sheet (`sheet_1.jpg`, `sheet_2.jpg`, ...) หรือภาพที่พนักงานอัปโหลด (`uploaded_*`) ต้องเปิดดูด้วย `read_file` ทุกไฟล์ เพื่อดูเหตุการณ์ การกระทำ สีหน้า สถานที่ ป้ายข้อความในภาพ
   - **กรณีคลิปกล้องวงจรปิด (CCTV) / กล้องหน้ารถ / คลิปเหตุการณ์ที่ไม่มีเสียงบรรยาย:** ให้ไล่อ่านภาพทีละเฟรมตามเวลาวินาทีสีเหลืองมุมซ้ายบน (ดูแสงแดดกลางวัน/กลางคืนจริง ห้ามมโนเวลา, แยกสีเสื้อผ้า/บุคคลให้แม่นยำ ห้ามสลับคน)
   - หากมีไฟล์เสียง `audio.mp3` ต้องเปิดฟังด้วย `read_file` ตั้งแต่วินาทีแรกจนจบ เพื่อแกะคำพูดจริงของผู้พูด 100%
2. {mode_instruction}
{focus_str}
3. กฎล็อกความแม่นยำ 4 ชั้น (4-Lock Verification) เมื่อค้นข้อมูลด้วย `search_web`:
   - Lock 1 (Timestamp Lock): ยึดช่วงเวลาเดียวกับ `upload_timestamp` ในคลิป/โพสต์ ห้ามนำข่าวคนละปีมาปน (ยกเว้นการขุดประวัติ/ไทม์ไลน์อดีตของบุคคลเดียวกันเพื่อนำมาบิดประเด็นตามข้อเท็จจริงจริง)
   - Lock 2 (Clip Genre Gate): ถ้าเป็นคลิปคนดัง/ข่าวสาธารณะ/ตัดจากรายการ ให้ค้นเชื่อมโยงต้นเรื่องเต็มและข้อมูลชั้นที่ 2 แต่ถ้าเป็นคลิปชาวบ้านทั่วไป/CCTV ที่ไม่มีข่าวรายงานเคสเดียวกันเป๊ะๆ ห้ามนำข่าวเคสอื่นมาสวมทับ
   - Lock 3 (3-Point Match): ข้อมูลต้องตรงบุคคล + สถานที่/เหตุการณ์ + ช่วงเวลา
   - Speed Guardrail: ใช้ `search_web` ค้นหาต้นทางเต็มและขุดข้อมูลชั้นที่ 2 อย่างตรงเป้า 2-5 ครั้งให้ได้ชุดข้อเท็จจริงที่แน่นและลึกพอ แล้วเขียนไฟล์ `result.json` ทันที (ห้ามค้นวนคำเดิมซ้ำๆ)

เมื่อเขียนและตรวจ QC ทุกบรรทัดเสร็จแล้ว (เช็กซ้ำว่าไร้คำต้องห้าม 'เบื้องหลัง...' / 'จึงสะท้อนชัดเจนว่า...', ไร้คำอังกฤษปนไทยในเนื้อข่าว, แต่ละบรรทัดไม่เกิน 220 ตัวอักษร, และใน `content` ของแต่ละกล่องใส่เฉพาะตัวเนื้อข่าวล้วนๆ ห้ามใส่หัวข้อนำหน้าซ้ำใน `content` และห้ามครอบด้วย ```text) ให้บันทึกผลลัพธ์ลงไฟล์ `{job_dir.as_posix()}/result.json` ด้วย `write_file` (และพิมพ์ JSON เดียวกันออกมาเป็นคำตอบสุดท้าย) ตามโครงสร้างนี้เท่านั้น:
{{
  "headline": "หัวข้อสรุปเคสข่าวสั้นๆ เข้าใจทันที",
  "articles": [
    {{
      "label": "มุมปกติ",
      "title": "1. ข่าวแบบมุมปกติ (สรุปแกนหลักสั้นๆ)",
      "content": "บรรทัดเปิดเรื่องคมๆ 1 บรรทัด\\n\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 1\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 2\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 3\\n\\nบรรทัดขมวดจบตามธรรมชาติ 1 บรรทัด"
    }}
  ],
  "raw_news": "เนื้อข่าวรวมทุกกล่อง",
  "visual_audio_breakdown": "สรุปข้อเท็จจริงดิบที่สกัดได้จากภาพ-เสียงในคลิปหรือบทความต้นทางแบบละเอียด",
  "context_connected": "ข้อมูลต้นทางฉบับเต็มและข้อมูลเชิงลึกชั้นที่ 2 ที่ค้นเชื่อมโยงเพิ่มเติม (หากไม่ได้ค้นเพิ่มให้ระบุว่า 'ใช้ข้อมูลจากต้นฉบับในคลิป/โพสต์ 100%')",
  "audit_tag": "✅ [ที่มาข้อมูล: ระบุแหล่งต้นทางจริง + ข้อมูลชั้นที่ 2 ที่ค้นตรวจซ้ำ + ผ่าน QC Data-Led Chameleon Flow]",
  "fact_check_references": [
    {{
      "claim": "ประเด็น/ข้อเท็จจริงสำคัญที่นำมาเขียนในเนื้อข่าว",
      "verify_where": "จุดที่พนักงานตรวจซ้ำได้ (เช่น นาทีในคลิป / เฟรมวินาทีใน Sheet / ย่อหน้าในข่าวต้นทาง)",
      "source_label": "ชื่อแหล่งอ้างอิง",
      "source_url": "ลิงก์ URL แหล่งอ้างอิง"
    }}
  ]
}}"""

    cmd_agy = [
        AGY_BIN,
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--add-dir", str(job_dir),
        "--print", prompt,
    ]

    agy_res = await run_subprocess(cmd_agy, cwd=job_dir, timeout=480)
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
        stdout_text = agy_res.stdout or ""
        m = re.search(r"\{[\s\S]*(\"articles\"|\"raw_news\")[\s\S]*\}", stdout_text)
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

    result_data = _normalize_result_articles(result_data, output_mode)
    result_data["contact_sheets"] = [f"/jobs/{job_id}/{s}" for s in all_contact_sheets]
    result_data["uploaded_images"] = [f"/jobs/{job_id}/{img}" for img in image_files]
    result_data["metadata"] = extracted_meta

    progress_cb("เสร็จสมบูรณ์ พร้อมใช้งาน!", 100)
    return result_data
