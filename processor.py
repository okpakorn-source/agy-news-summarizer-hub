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


def resolve_social_url(url: str) -> str:
    """Resolve Facebook /share/... or short social links to their canonical URL before classification/yt-dlp."""
    u_low = url.lower()
    if any(d in u_low for d in ("facebook.com/share/", "fb.com/share/", "fb.watch", "vt.tiktok.com", "vm.tiktok.com")):
        is_fb = any(d in u_low for d in ("facebook.com", "fb.com", "fb.watch"))
        ua = (
            "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
            if is_fb
            else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        try:
            r = requests.get(url, headers={"User-Agent": ua}, allow_redirects=True, timeout=10)
            if r.url and r.url != url:
                cleaned = re.sub(r"[?&](rdid|share_url|utm_[^=&]+|_rdr|fs|mibextid)=[^&]*", "", r.url).rstrip("?&/")
                return cleaned or r.url
        except Exception:
            pass
    return url


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
                    img_ua = (
                        "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
                        if ("fbsbx.com" in og_image or is_fb)
                        else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    )
                    ir = requests.get(og_image, headers={"User-Agent": img_ua}, timeout=15)
                    if ir.status_code == 200 and ir.content and len(ir.content) > 1500:
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
    # Remove accidental episode codes like EP.981 or EP 981
    t = re.sub(r"\bEP\.?\s*\d+\b", "", t, flags=re.IGNORECASE)
    # Clean banned robotic template prefixes if LLM accidentally slips
    lines = t.splitlines()
    cleaned_lines: List[str] = []
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            cleaned_lines.append("")
            continue
        # Strip accidental bullet points or fake section headers
        s = re.sub(r"^[•\-\*]\s+", "", s)
        # Replace banned opening/closing template phrases with natural flow
        s = re.sub(r"^เบื้องหลัง(ของ|คำพูดของ|เรื่องราวของ|ความสำเร็จของ)\s*", "", s)
        s = re.sub(r"^หากย้อนกลับไป(เมื่อ|ใน|ช่วง)", r"\1", s)
        s = re.sub(r"^(เหตุการณ์นี้เริ่มต้นขึ้นเมื่อ|จุดเริ่มต้นมาจาก|ต้นเรื่องเกิดจาก)\s*", "เกิดขึ้นเมื่อ", s) if s.startswith(("เหตุการณ์นี้เริ่มต้นขึ้นเมื่อ", "จุดเริ่มต้นมาจากเมื่อ")) else s
        s = re.sub(r"จึงสะท้อนชัดเจนว่า", "ทำให้เห็นว่า", s)
        s = re.sub(r"จึงเป็นบทเรียนชีวิตจริงว่า", "ทำให้คนอ่านได้เห็นว่า", s)
        s = re.sub(r"จึงเป็นเครื่องพิสูจน์(ที่มีชีวิต)?ว่า", "ตอกย้ำให้เห็นว่า", s)
        s = re.sub(r"^นี่แหละ(คือ)?\s*", "", s)
        s = re.sub(r"  +", " ", s).strip()
        cleaned_lines.append(s)
    return "\n".join(cleaned_lines).strip()


SKILL_SET_REGISTRY: Dict[str, Dict[str, str]] = {
    "skill_set_1": {
        "id": "skill_set_1",
        "short_tag": "ชุดสกิลที่ 1: ไทม์ไลน์อดีต",
        "full_badge": "[ชุดสกิลที่ 1: มาตรฐานเดิม — รีเสิร์ช 2 ชั้น & ย้อนไทม์ไลน์]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-1-classic-timeline\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[ชุดสกิลที่ 1: มาตรฐานเดิม — รีเสิร์ช 2 ชั้น & ย้อนไทม์ไลน์ (`news-skill-set-1-classic-timeline`)]** เท่านั้น (ห้ามปนกับชุดสกิลอื่น):\n"
            "    - การรีเสิร์ช: สเต็ป 1 ค้นหาต้นทางฉบับเต็มเก็บข้อเท็จจริงครบทุกฝ่าย (Room 1) + สเต็ป 2 ค้นย้อนไทม์ไลน์ภาพจำในอดีต ผลงานจริง หรือเหตุการณ์สำคัญก่อนหน้าตามลำดับปี พ.ศ. (Room 2)\n"
            "    - มุมปกติ (Room 1): เปิดด้วยคำพูดจริงหรือเหตุการณ์ตั้งต้น -> บล็อกกลาง 4-5 บรรทัดไล่ต้นเหตุ กลไก/เหตุผล คำชี้แจงทุกฝ่าย และจุดคลายปม -> ขมวดจบตามธรรมชาติ\n"
            "    - มุมบิดประเด็น (Room 2): พาย้อนไทม์ไลน์ภาพจำในอดีต 3-4 ฉากสำคัญตามลำดับเวลาจริง เพื่อพิสูจน์ตัวตนและศักดิ์ศรีด้วยการกระทำจริง (ห้ามใช้ข้อกฎหมายมาตราแข็งๆ ของชุดที่ 2 และห้ามซ้ำฉากกับมุมปกติเกิน 1 บรรทัด)"
        ),
    },
    "skill_set_2": {
        "id": "skill_set_2",
        "short_tag": "ชุดสกิลที่ 2: ผ่าข้อกฎหมาย/ระเบียบรัฐ",
        "full_badge": "[ชุดสกิลที่ 2: ผ่ากลไกข้อกฎหมาย & เอกสารราชการต้นขั้ว]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-2-legal-forensic\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[ชุดสกิลที่ 2: ผ่ากลไกข้อกฎหมาย & เอกสารราชการต้นขั้ว (`news-skill-set-2-legal-forensic`)]** เท่านั้น (ห้ามปนกับชุดสกิลอื่น):\n"
            "    - การรีเสิร์ช: สเต็ป 1 ค้นต้นทางฉบับเต็มแยกคำอ้างของแต่ละฝ่ายออกมาเป็นข้อๆ + สเต็ป 2 ค้นเจาะตัวบทกฎหมายจริง (เช่น ประมวลกฎหมายแพ่งและพาณิชย์ มาตรา 110-136, ประมวลรัษฎากร มาตรา 37/40, ระเบียบกรมการปกครอง/สรรพากร) และค้นข้อมูลทะเบียนนิติบุคคล/มูลนิธิจริง\n"
            "    - มุมปกติ (Room 1): เปิดด้วยจุดปะทะของข้อกล่าวหาและหลักฐานสำคัญ -> บล็อกกลาง 4-5 บรรทัดผ่ากลไกกฎหมายและข้อบังคับจริงว่าทำไมแต่ละฝ่ายต้องทำแบบนั้น -> ขมวดจบที่จุดชี้ขาดทางเอกสาร\n"
            "    - มุมบิดประเด็น (Room 2): ใช้เกราะข้อกฎหมายและทะเบียนเอกสารราชการต้นขั้ว 3-4 ด่านมาพิสูจน์ความบริสุทธิ์หรือชี้จุดตายของคดี (ห้ามเล่าย้อนไทม์ไลน์ดราม่าอดีตแบบชุดที่ 1)"
        ),
    },
    "skill_set_3": {
        "id": "skill_set_3",
        "short_tag": "ชุดสกิลที่ 3: จับคู่เทียบตัวเลข/ท่าที",
        "full_badge": "[ชุดสกิลที่ 3: จับคู่เทียบตัวเลข & ท่าทีตัดกันแรง]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-3-fact-collision\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[ชุดสกิลที่ 3: จับคู่เทียบตัวเลข & ท่าทีตัดกันแรง (`news-skill-set-3-fact-collision`)]** เท่านั้น (ห้ามปนกับชุดสกิลอื่น):\n"
            "    - การรีเสิร์ช: สเต็ป 1 สกัดตัวเลขทุกตัวและลำดับการกระทำของทุกฝ่ายจากต้นทางฉบับเต็ม + สเต็ป 2 ค้นหาตัวเลขคู่ขนานในอดีต (เช่น ยอดบริจาคส่วนตัว ยอดช่วยภัยพิบัติ หรือท่าทีของอีกฝั่งในเวลาเดียวกัน) เพื่อนำมาวางเทียบกันแบบหมัดต่อหมัด\n"
            "    - มุมปกติ (Room 1): เปิดด้วยการวาง 2 ตัวเลขหรือ 2 เหตุการณ์ที่ขัดแย้งกันในประโยคเดียว -> บล็อกกลาง 4-5 บรรทัดเดินเรื่องด้วยการเปรียบเทียบคู่ขนานทุกบรรทัด -> ขมวดจบด้วยการเทียบผลลัพธ์สุดท้ายของสองฝั่ง\n"
            "    - มุมบิดประเด็น (Room 2): จับคู่เทียบท่าทีและวิถีชีวิตที่ต่างกันสุดขั้วของ 2 ฝั่ง (เช่น ฝั่งหนึ่งเก็บตัวเงียบเผชิญความจริงตรงหน้า vs อีกฝั่งเคลื่อนไหวอีกแบบ พร้อมเทียบตัวเลขการเสียสละจริง 3-4 คู่โดยไม่ต้องดราม่าฟูมฟาย)"
        ),
    },
    "skill_set_4": {
        "id": "skill_set_4",
        "short_tag": "ชุดสกิลที่ 4: เจาะรหัส 1 ประโยคขยี้ใจ",
        "full_badge": "[ชุดสกิลที่ 4: เจาะรหัส 1 ประโยคขยี้หัวใจมนุษย์]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-4-micro-quote-decoder\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[ชุดสกิลที่ 4: เจาะรหัส 1 ประโยคขยี้หัวใจมนุษย์ (`news-skill-set-4-micro-quote-decoder`)]** เท่านั้น (ห้ามปนกับชุดสกิลอื่น):\n"
            "    - การรีเสิร์ช: สเต็ป 1 อ่าน/ฟังต้นทางฉบับเต็มเพื่อคัดเลือก '1 ประโยคสั้นๆ ที่สะเทือนใจหรือมีนัยสำคัญที่สุด' + สเต็ป 2 ค้นหาความหมายเบื้องหลังคำพูดนั้นจากประวัติการต่อสู้ บทสัมภาษณ์เก่า หรือความผูกพันกับคนรอบข้าง 3-4 เหตุการณ์จริง\n"
            "    - มุมปกติ (Room 1): เปิดด้วยคำพูดจริง 1 ประโยคที่คมที่สุด -> บล็อกกลาง 4-5 บรรทัดถอดรหัสทีละเปลาะว่าทำไมเขาถึงพูดคำนี้ออกมา และมีต้นเหตุ/กลไกชีวิตจริงอะไรรองรับ -> ขมวดจบด้วยความหมายที่แท้จริงของประโยคนั้น\n"
            "    - มุมบิดประเด็น (Room 2): เจาะหัวใจและศักดิ์ศรีของผู้พูดผ่านคำพูดสั้นๆ เพียงประโยคเดียว แล้วกางหลักฐานชีวิตจริง 4 บรรทัดกลางที่พิสูจน์ว่าเขาทำตามคำพูดนั้นมาตลอดอย่างไร"
        ),
    },
    "skill_set_5": {
        "id": "skill_set_5",
        "short_tag": "ชุดสกิลที่ 5: ลูกผสม 3 มิติขั้นสุด ⭐",
        "full_badge": "[ชุดสกิลที่ 5: ลูกผสม 3 มิติขั้นสุด (ต้นทางครบ + เอกสารรัฐ/กฎหมาย + หัวใจมนุษย์)]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-5-ultimate-3d-hybrid\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[ชุดสกิลที่ 5: ลูกผสม 3 มิติขั้นสุด (`news-skill-set-5-ultimate-3d-hybrid`)]** เท่านั้น (ห้ามปนกับชุดสกิลอื่น):\n"
            "    - การรีเสิร์ช 3 มิติ: มิติที่ 1 ค้นต้นทางฉบับเต็มเก็บคำพูดและตัวเลขครบทุกฝ่าย + มิติที่ 2 ค้นเอกสารราชการ/ทะเบียนมูลนิธิ/ข้อกฎหมายหรือระบบตรวจสอบจริงของรัฐ + มิติที่ 3 ค้นประวัติการกระทำจริงในอดีตที่พิสูจน์หัวใจและศักดิ์ศรี\n"
            "    - มุมปกติ (Room 1): ผสานคำพูดจริงต้นทาง + กลไกกฎหมาย/ระบบตรวจสอบจริง ใน 4-5 บรรทัดกลางที่แน่นครบวงจร 100%\n"
            "    - มุมบิดประเด็น (Room 2): ผสานหลักฐานทะเบียนราชการ/ข้อบังคับจริง + ภาพจำการลงมือทำจริงในอดีต + คำมั่นสัญญาที่ยืนหยัดรอพิสูจน์ความจริง (ให้คนที่รักและติดตามอ่านแล้วอุ่นใจบนหลักฐานจริง 100%)"
        ),
    },
}


def get_skill_set_info(skill_set: str) -> Dict[str, str]:
    return SKILL_SET_REGISTRY.get(skill_set) or SKILL_SET_REGISTRY["skill_set_5"]


def _normalize_result_articles(result_data: Dict[str, Any], output_mode: str, skill_set: str = "skill_set_5") -> Dict[str, Any]:
    s_info = get_skill_set_info(skill_set)
    short_tag = s_info["short_tag"]
    full_badge = s_info["full_badge"]

    result_data["skill_set"] = s_info["id"]
    result_data["skill_set_short"] = short_tag
    result_data["skill_set_label"] = full_badge

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
                normalized.append({
                    "label": label,
                    "title": title,
                    "skill_set": s_info["id"],
                    "skill_set_short": short_tag,
                    "skill_set_label": full_badge,
                    "content": content,
                })

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
            "skill_set": s_info["id"],
            "skill_set_short": short_tag,
            "skill_set_label": full_badge,
            "content": cleaned_raw,
        })

    result_data["articles"] = normalized
    if len(normalized) == 1:
        result_data["raw_news"] = f"{full_badge}\n\n{normalized[0]['content']}"
    elif len(normalized) > 1:
        result_data["raw_news"] = f"{full_badge}\n\n" + "\n\n══════════════════════════════\n\n".join(
            f"【{a['title']}】\n\n{a['content']}" for a in normalized
        )
    else:
        result_data["raw_news"] = cleaned_raw

    audit = str(result_data.get("audit_tag") or "").strip()
    if short_tag not in audit:
        result_data["audit_tag"] = f"{full_badge} • {audit}" if audit else f"✅ {full_badge}"

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
    skill_set: str = "skill_set_5",
) -> Dict[str, Any]:
    """
    Full pipeline:
    1. Download video(s) via yt-dlp & extract metadata + audio + timestamped frame contact sheets.
    2. Scrape any article/webpage URLs.
    3. Invoke Antigravity CLI (agy.exe) on the prepared job folder using ONLY the selected Skill Set (1-5).
    """
    s_info = get_skill_set_info(skill_set)
    job_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    extracted_meta: Dict[str, Any] = {
        "job_id": job_id,
        "submitted_urls": urls,
        "employee_raw_text": raw_text,
        "employee_focus_instruction": focus_instruction,
        "output_mode": output_mode,
        "skill_set": s_info["id"],
        "skill_set_label": s_info["full_badge"],
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
            target_url = await asyncio.to_thread(resolve_social_url, url)
            if is_video_url(target_url):
                # Also try extracting Thai subtitles/auto-captions on YouTube for fast, complete transcript of long videos
                if any(d in target_url.lower() for d in ("youtube.com", "youtu.be")):
                    try:
                        await run_subprocess(
                            ["yt-dlp", "--write-auto-sub", "--write-sub", "--sub-lang", "th", "--skip-download", "-o", "sub", target_url],
                            cwd=job_dir,
                            timeout=30,
                        )
                    except Exception:
                        pass
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
                    target_url,
                ]
                try:
                    res = await run_subprocess(cmd, cwd=job_dir, timeout=120)
                except Exception:
                    res = None

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

    progress_cb(f"AI กำลังรัน {s_info['short_tag']} (ค้นข้อมูล & เขียนข่าว)...", 65)

    # Load ONLY the selected standalone Skill Set into SKILL_GUIDES.md so the 5 skill sets never overlap!
    selected_skill_path = Path(s_info["skill_path"])
    writer_skill_path = Path(r"C:\Users\User\.gemini\config\skills\viral-news-writer\SKILL.md")

    guides_parts: List[str] = []
    if selected_skill_path.exists():
        guides_parts.append(
            f"=== คู่มือชุดสกิลที่เลือกใช้งาน: {s_info['full_badge']} ===\n"
            + selected_skill_path.read_text(encoding="utf-8")
        )
    if output_mode == "viral_3_para" and writer_skill_path.exists():
        guides_parts.append("=== คู่มือสกิล `viral-news-writer` ===\n" + writer_skill_path.read_text(encoding="utf-8"))

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
        f"  • ชุดสกิลที่บังคับใช้ในงานนี้ (ห้ามปนกับชุดสกิลอื่นเด็ดขาด): **{s_info['full_badge']}**\n"
        f"  • หลักการเฉพาะของชุดสกิลนี้:\n{s_info['method_instruction']}\n"
        "  • มาตรฐานการเขียนและการเล่าเรื่อง (เล่าแบบข้อมูลแน่น เต็มอิ่ม ห้ามตัดห้วน):\n"
        "    - โครงสร้างมาตรฐาน 3 บล็อก (ยกเว้นโหมด 3 ย่อหน้า): บล็อก 1 (1 บรรทัดเดี่ยวเปิดเรื่องคมๆ) -> เว้นบรรทัดว่าง `\\n\\n` -> บล็อก 2 (เนื้อหาเจาะลึกรายจุด แยกทีละบรรทัด 4–5 บรรทัดกลาง ข้อมูลแน่นครบวงจร) -> เว้นบรรทัดว่าง `\\n\\n` -> บล็อก 3 (ขมวดจบตามธรรมชาติของเรื่อง 1 บรรทัด)\n"
        "    - แบนคำแม่พิมพ์นักวิเคราะห์ซ้ำซาก 100%: ห้ามเปิดเรื่องด้วย 'เบื้องหลัง...', 'เหตุผลที่... มีที่มาจาก...', 'เส้นทางพลิกผันของ...', 'ท่ามกลางกระแส...' ห้ามขึ้นกลางเรื่องด้วย 'หากย้อนกลับไป...', 'จุดเริ่มต้นมาจาก...', 'เหตุการณ์นี้เริ่มต้นขึ้นเมื่อ...', 'ในทางธุรกิจ...' และห้ามจบเทศนาสั่งสอน ('จึงสะท้อนชัดเจนว่า...', 'สิ่งนี้สะท้อน...', 'จึงเป็นบทเรียนชีวิตจริงว่า...', 'จึงเป็นเครื่องพิสูจน์ว่า...', 'นี่แหละ...', 'จาก... กลายเป็น...')\n"
        "    - กฎแยกห้องข้อมูลเด็ดขาด (`Strict Room Separation / Zero Scene Duplication`) เมื่อมีมากกว่า 1 กล่องข่าว (เช่น มุมปกติ + บิดประเด็น): ห้ามนำฉากเหตุการณ์ ลำดับเวลา หรือรายชื่อในเหตุการณ์ที่ใช้เล่าในกล่อง `มุมปกติ` ไปแล้ว มาเขียนเล่าซ้ำในกล่อง `บิดประเด็น` เกิน 1 บรรทัดเด็ดขาด! กล่อง `บิดประเด็น` ต้องเป็น 'เรื่องจริงอีกห้องหนึ่ง (Room 2)' ที่ค้นเจาะลึกมาจาก `search_web` ตามหลักของชุดสกิลนี้โดยเฉพาะ และทุกกล่องต้องมีท่าเปิดและท่าจบต่างโครงสร้างกัน 100%\n"
        "    - ความยาวบรรทัดมือถือ: แต่ละบรรทัดกระชับอ่านลื่น ~120-195 ตัวอักษร (ห้ามยาวเกิน ~220 ตัวอักษร)\n"
        "    - กฎ 95/5 & Pre-Generator Filter & Meta Safety 100%: ปูสะพานเชื่อมแค่ครึ่งถึง 1 บรรทัดให้คนไม่ตามข่าวเข้าใจทันที ห้ามพ่วงคีย์เวิร์ดข่าวใหญ่เก่าที่ไม่เกี่ยว ห้ามใส่รหัสเทป EP.xxx หรือวันที่ราชการแข็งๆ ตัดคำอังกฤษปนไทยในเนื้อข่าวออก 100% และงดถ้อยคำที่ส่อผิดกฎชุมชน Meta 100%"
    )

    if output_mode == "strict_clip_only":
        mode_instruction = (
            f"โหมดที่เลือก: แกะเฉพาะในคลิป/โพสต์ 100% ด้วย {s_info['full_badge']} (`strict_clip_only` -> สร้าง 1 กล่องข่าวใน `articles` โดย `label` = `'เฉพาะในคลิป 100%'`)\n"
            "  • ใช้เฉพาะข้อมูลที่ได้จากคลิป/ภาพ/ข้อความต้นทาง 100% เท่านั้น (ห้ามใช้ `search_web` ค้นหาข่าวภายนอกมาปนเด็ดขาด) แล้วสรุปใจความข่าวครบวงจรตามโครงสร้าง 3 บล็อกของชุดสกิลที่เลือก"
        )
    elif output_mode == "viral_3_para":
        mode_instruction = (
            f"โหมดที่เลือก: ข่าวสั้นไวรัล 3 ย่อหน้า ด้วย {s_info['full_badge']} (`viral_3_para` -> สร้าง 1 กล่องข่าวใน `articles` โดย `label` = `'ข่าวสั้น 3 ย่อหน้า'`)\n"
            "  • ค้นเช็กข้อเท็จจริงตามแนวทางของชุดสกิลที่เลือก แล้วเขียนด้วยสกิล `viral-news-writer` (3 ย่อหน้ากระชับ จับ 1 ประเด็นบริสุทธิ์ เปิดคมไร้คำเกริ่นเรียงความ จบด้วยข้อเท็จจริงคมๆ)"
        )
    elif output_mode == "normal_1_spin_1":
        mode_instruction = (
            f"โหมดที่เลือก: **มุมปกติ 1 ข่าว + บิดประเด็น 1 ข่าว** ด้วย **{s_info['full_badge']}** (`normal_1_spin_1` -> สร้าง **2 กล่องข่าวแยกกัน** ในอาร์เรย์ `articles`):\n"
            f"  1. กล่องที่ 1 (`label`: `'มุมปกติ'`, `title`: `'1. ข่าวแบบมุมปกติ — {s_info['short_tag']}'`): สรุปใจความข่าวหลักครบวงจรตามหลักมุมปกติของ {s_info['short_tag']} (Room 1 — โครงสร้าง 3 บล็อก ช่วงกลาง 4-5 บรรทัดข้อมูลแน่นครบ)\n"
            f"  2. กล่องที่ 2 (`label`: `'บิดประเด็น'`, `title`: `'2. ข่าวแบบบิดประเด็น — {s_info['short_tag']}'`): บิดประเด็นตามหลักมุมบิดของ {s_info['short_tag']} — ยึดแนวที่พนักงานสั่งเป็นแกนหลัก โดยต้องใช้ `search_web` ขุดข้อมูลจริงชุดใหม่ที่เป็น **คนละห้องข้อมูลกับกล่องที่ 1 (Room 2)** ห้ามเอาฉากเหตุการณ์ในกล่องที่ 1 มาเล่าซ้ำเกิน 1 บรรทัดเด็ดขาด!\n"
            f"{core_rules}"
        )
    elif output_mode == "normal_1_spin_2":
        mode_instruction = (
            f"โหมดที่เลือก: **มุมปกติ 1 ข่าว + บิดประเด็น 2 ข่าว** ด้วย **{s_info['full_badge']}** (`normal_1_spin_2` -> สร้าง **3 กล่องข่าวแยกกัน** ในอาร์เรย์ `articles`):\n"
            f"  1. กล่องที่ 1 (`label`: `'มุมปกติ'`, `title`: `'1. ข่าวแบบมุมปกติ — {s_info['short_tag']}'`): สรุปใจความข่าวหลักตามหลักของ {s_info['short_tag']} (Room 1)\n"
            f"  2. กล่องที่ 2 (`label`: `'บิดประเด็น 1'`, `title`: `'2. ข่าวแบบบิดประเด็น มุมที่ 1 — {s_info['short_tag']}'`): บิดประเด็นมุมที่ 1 ตามแนวความต้องการของพนักงานด้วยหลักของ {s_info['short_tag']} โดยเป็นข้อมูลคนละห้องกับกล่องที่ 1 (Room 2)\n"
            f"  3. กล่องที่ 3 (`label`: `'บิดประเด็น 2'`, `title`: `'3. ข่าวแบบบิดประเด็น มุมที่ 2 — {s_info['short_tag']}'`): บิดประเด็นมุมที่ 2 ที่ฉีกคนละมุม/คนละห้องข้อมูลกับกล่องที่ 1 และ 2 อย่างสิ้นเชิง (Room 3)\n"
            f"{core_rules}"
        )
    elif output_mode == "spin_only_1":
        mode_instruction = (
            f"โหมดที่เลือก: **บิดประเด็นล้วน 1 ข่าว** ด้วย **{s_info['full_badge']}** (`spin_only_1` -> สร้าง **1 กล่องข่าว** ในอาร์เรย์ `articles` โดย `label` = `'บิดประเด็น'`):\n"
            f"  • เจาะมุมบิดตามหลักของ {s_info['short_tag']} และความต้องการของพนักงาน โดยมีสะพานปูที่มาข่าวหลักสั้นๆ ครึ่งถึง 1 บรรทัด (กฎ 95/5) และกางข้อมูลจริงแน่นครบ 4-5 บรรทัดกลาง\n"
            f"{core_rules}"
        )
    else:
        mode_instruction = (
            f"โหมดที่เลือก: **มุมปกติ 1 ข่าว** ด้วย **{s_info['full_badge']}** (`standard_4lock` -> สร้าง **1 กล่องข่าว** ในอาร์เรย์ `articles` โดย `label` = `'มุมปกติ'` สรุปใจความข่าวตามหลักของ {s_info['short_tag']}, ยกเว้นกรณีที่ข้อความคำสั่งของพนักงานด้านล่างระบุชัดเจนว่าขอหลายข่าวหรือขอบิดประเด็นเพิ่ม ให้สร้างจำนวนกล่องใน `articles` ตามคำสั่งพนักงานได้เลย!):\n"
            f"{core_rules}"
        )

    focus_str = (
        f"\n🔥 **คำสั่ง/ความต้องการพิเศษจากพนักงาน (สำคัญสูงสุด — ต้องทำตามแนวทางและจำนวนข่าวที่สั่งให้ครบถ้วนแม่นยำภายใต้ {s_info['full_badge']}):**\n"
        f"\"{focus_instruction}\"\n"
        if focus_instruction
        else ""
    )

    prompt = f"""คุณคือระบบวิเคราะห์ สรุปใจความข่าว และบิดประเด็นข่าวไวรัลความแม่นยำสูง โดยงานนี้เลือกใช้ **{s_info['full_badge']}** เพียงชุดเดียวเท่านั้น (แยกทักษะชัดเจน ห้ามปนกับชุดสกิลอื่น)
ในโฟลเดอร์งาน `{job_dir.as_posix()}` มีไฟล์ที่เตรียมไว้เรียบร้อยแล้ว ได้แก่: {files_list_str}

ขั้นตอนการทำงานบังคับ:
1. ใช้เครื่องมือ `read_file` เปิดอ่านไฟล์ทั้งหมดในรายการด้านบนเป็นอันดับแรก:
   - **ต้องอ่าน `SKILL_GUIDES.md` เป็นอันดับแรก** เพื่อยึดกฎการรีเสิร์ช โครงสร้างมุมปกติ โครงสร้างมุมบิด และกฎข้อห้ามของ **{s_info['full_badge']}** แบบ 100%
   - อ่าน `job_context.json` เพื่อดูวันเวลาโพสต์จริง (`upload_timestamp`), แคปชัน, ชื่อผู้โพสต์ และคำสั่งจากพนักงาน
   - หากมีไฟล์ภาพ Contact Sheet (`sheet_1.jpg`, `sheet_2.jpg`, ...) หรือภาพที่พนักงานอัปโหลด (`uploaded_*`) ต้องเปิดดูด้วย `read_file` ทุกไฟล์ เพื่อดูเหตุการณ์ การกระทำ สีหน้า สถานที่ ป้ายข้อความในภาพ
   - **กรณีคลิปกล้องวงจรปิด (CCTV) / กล้องหน้ารถ / คลิปเหตุการณ์ที่ไม่มีเสียงบรรยาย:** ให้ไล่อ่านภาพทีละเฟรมตามเวลาวินาทีสีเหลืองมุมซ้ายบน (ดูแสงแดดกลางวัน/กลางคืนจริง ห้ามมโนเวลา, แยกสีเสื้อผ้า/บุคคลให้แม่นยำ ห้ามสลับคน)
   - หากมีไฟล์เสียง `audio.mp3` ต้องเปิดฟังด้วย `read_file` ตั้งแต่วินาทีแรกจนจบ เพื่อแกะคำพูดจริงของผู้พูด 100%
2. {mode_instruction}
{focus_str}
3. กฎล็อกความแม่นยำ 4 ชั้น (4-Lock Verification) เมื่อค้นข้อมูลด้วย `search_web`:
   - Lock 1 (Timestamp Lock): ยึดช่วงเวลาเดียวกับ `upload_timestamp` ในคลิป/โพสต์ ห้ามนำข่าวคนละปีมาปน (ยกเว้นการขุดประวัติ/ไทม์ไลน์อดีตของบุคคลเดียวกันเพื่อนำมาบิดประเด็นตามข้อเท็จจริงจริง)
   - Lock 2 (Clip Genre Gate): ถ้าเป็นคลิปคนดัง/ข่าวสาธารณะ/ตัดจากรายการ หรือมีชื่อบุคคล/เพจปรากฏชัด ให้ค้นเชื่อมโยงต้นเรื่องเต็ม (ชั้นที่ 1) และค้นข้อมูลเจาะลึกตามแนวทางของ **{s_info['short_tag']}** (ชั้นที่ 2) เสมอ แต่ถ้าเป็นคลิปชาวบ้านทั่วไป/CCTV ที่ไม่มีชื่อบุคคลและไม่มีข่าวรายงานเคสเดียวกันเป๊ะๆ ห้ามนำข่าวเคสอื่นมาสวมทับ
   - Lock 3 (3-Point Match): ข้อมูลต้องตรงบุคคล + สถานที่/เหตุการณ์ + ช่วงเวลา (ระวังคนชื่อคล้ายกันคนละอาชีพ เช่น แยกชื่อ-นามสกุลจริงจากป้ายชื่อ/เอกสารให้แม่นยำ)
   - Lock 4 (Mandatory Skill-Specific Search Execution): ในทุกโหมด (ยกเว้น `strict_clip_only`) **ต้องเรียกใช้ `search_web` จริงเสมออย่างน้อย 2–5 ครั้ง** ตามขั้นตอนการรีเสิร์ชใน `SKILL_GUIDES.md` ของ **{s_info['short_tag']}** ห้ามหยุดค้นแค่ชั้นเดียวแล้วเอาเนื้อหากล่องแรกมาแบ่งครึ่งเขียนซ้ำเด็ดขาด!

เมื่อเขียนและตรวจ QC ทุกบรรทัดเสร็จแล้ว (เช็กซ้ำว่าไร้คำต้องห้าม 'เบื้องหลัง...' / 'จึงสะท้อนชัดเจนว่า...', ไร้คำอังกฤษปนไทยในเนื้อข่าว, แต่ละบรรทัดไม่เกิน 220 ตัวอักษร, และใน `content` ของแต่ละกล่องใส่เฉพาะตัวเนื้อข่าวล้วนๆ ห้ามใส่หัวข้อนำหน้าซ้ำใน `content` และห้ามครอบด้วย ```text) ให้บันทึกผลลัพธ์ลงไฟล์ `{job_dir.as_posix()}/result.json` ด้วย `write_file` (และพิมพ์ JSON เดียวกันออกมาเป็นคำตอบสุดท้าย) ตามโครงสร้างนี้เท่านั้น:
{{
  "headline": "หัวข้อสรุปเคสข่าวสั้นๆ เข้าใจทันที",
  "skill_set": "{s_info['id']}",
  "skill_set_label": "{s_info['full_badge']}",
  "articles": [
    {{
      "label": "มุมปกติ",
      "title": "1. ข่าวแบบมุมปกติ — {s_info['short_tag']}",
      "content": "บรรทัดเปิดเรื่องคมๆ 1 บรรทัด\\n\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 1\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 2\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 3\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 4\\n\\nบรรทัดขมวดจบตามธรรมชาติ 1 บรรทัด"
    }}
  ],
  "raw_news": "เนื้อข่าวรวมทุกกล่อง",
  "visual_audio_breakdown": "สรุปข้อเท็จจริงดิบที่สกัดได้จากภาพ-เสียงในคลิปหรือบทความต้นทางแบบละเอียด",
  "context_connected": "ระบุผลลัพธ์การค้นหาจริงตามหลักของ {s_info['short_tag']} แยกเป็น (ชั้นที่ 1: ข้อมูลต้นทางฉบับเต็ม) และ (ชั้นที่ 2: ข้อมูลเจาะลึกที่ค้นเจอจริงจาก search_web)",
  "audit_tag": "✅ {s_info['full_badge']} • [ที่มาข้อมูล: ระบุแหล่งต้นทางจริง + ข้อมูลชั้นที่ 2 ที่ค้นตรวจซ้ำ]",
  "fact_check_references": [
    {{
      "claim": "ประเด็น/ข้อเท็จจริงสำคัญที่นำมาเขียนในเนื้อข่าว",
      "verify_where": "จุดที่พนักงานตรวจซ้ำได้ (เช่น นาทีในคลิป / เฟรมวินาทีใน Sheet / ย่อหน้าในข่าวต้นทาง / มาตรากฎหมาย)",
      "source_label": "ชื่อแหล่งอ้างอิงจริง",
      "source_url": "ลิงก์ URL เต็มของโพสต์หรือบทความที่ใช้อ้างอิง (หากไม่มีลิงก์ย่อยให้ใช้ลิงก์ต้นทางที่ส่งเข้ามาร่วมกับคำค้นตรวจสอบ)"
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
            "audit_tag": f"✅ {s_info['full_badge']} • ประมวลผลผ่าน Antigravity Agent บนเครื่องแม่",
        }

    result_data = _normalize_result_articles(result_data, output_mode, skill_set=s_info["id"])
    result_data["contact_sheets"] = [f"/jobs/{job_id}/{s}" for s in all_contact_sheets]
    result_data["uploaded_images"] = [f"/jobs/{job_id}/{img}" for img in image_files]
    result_data["metadata"] = extracted_meta

    return result_data
