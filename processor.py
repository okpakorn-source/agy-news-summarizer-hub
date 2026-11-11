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


def _get_top_matched_254_prompts(query_text: str, top_k: int = 6) -> str:
    """Fast in-process matcher against prompts_254.json to inject top DNA cards directly into SKILL_GUIDES.md."""
    try:
        p_path = Path(r"C:\Users\User\.gemini\config\skills\viral-prompt-library\prompts_254.json")
        if not p_path.exists():
            return ""
        raw = json.loads(p_path.read_text(encoding="utf-8"))
        items = raw.get("prompts") if isinstance(raw, dict) and "prompts" in raw else (list(raw.values()) if isinstance(raw, dict) else raw)
        if not isinstance(items, list):
            return ""

        tokens = [w for w in re.split(r"[\s,./!?:;\"'()\[\]\-]+", (query_text or "").lower()) if len(w) >= 2]
        scored = []
        for card_num, p in enumerate(items, 1):
            if not isinstance(p, dict):
                continue
            blob = " ".join(
                str(p.get(k) or "")
                for k in ("promptName", "category", "emotionalType", "hookStyle", "structure", "ctaStyle", "promptText")
            ).lower()
            score = float(p.get("viralScore") or 80) * 0.15
            for tk in tokens[:40]:
                if tk in (p.get("promptName") or "").lower():
                    score += 12.0
                elif tk in blob:
                    score += 3.5
            scored.append((score, card_num, p))

        scored.sort(key=lambda x: x[0], reverse=True)
        out_lines = ["=== 🧬 การ์ด DNA แนะนำจากคลัง 254 Prompts (คัดกรองตรงเนื้อหาข่าวนี้โดยอัตโนมัติ) ==="]
        for idx, (_, card_num, card) in enumerate(scored[:top_k], 1):
            cid = card.get("id", "")
            cname = card.get("promptName", "")
            cat = card.get("category", "")
            hook = str(card.get("hookStyle") or "")[:180]
            struct = str(card.get("structure") or "")[:240]
            cta = str(card.get("ctaStyle") or "")[:160]
            ex_hooks = card.get("exampleHooks") or []
            ex_str = (" | ".join(str(x) for x in ex_hooks[:2]) if isinstance(ex_hooks, list) else str(ex_hooks))[:220]
            out_lines.append(
                f"{idx}. **การ์ด #{card_num} [{cid}] {cname}** (หมวด: {cat})\n"
                f"   - ท่าเปิด (Hook Style): {hook}\n"
                f"   - โครงสร้าง (Structure): {struct}\n"
                f"   - ท่าปิด (Closing Style): {cta}\n"
                f"   - ตัวอย่างขึ้นต้น: {ex_str}"
            )
        return "\n".join(out_lines)
    except Exception:
        return ""


SKILL_SET_REGISTRY: Dict[str, Dict[str, str]] = {
    "all_5_styles": {
        "id": "all_5_styles",
        "short_tag": "🔥 ครบทั้ง 2 แนวหลัก (3 ย่อหน้าเปี่ยมคุณค่า 254 DNA + สกิลลูกเล่นแตกบรรทัด)",
        "full_badge": "[มาตรฐาน 2 แนวหลัก 98/100: (1) โหมด 3 ย่อหน้ากระชับเปี่ยมคุณค่า 1+1+1 โมเดล จิ๊ก เนาวรัตน์ จับคู่ 254 DNA + (2) สกิลลูกเล่นประยุกต์ข้อมูล + ไฮบริดแตกบรรทัด]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-6-applied-hook\SKILL.md",
        "method_instruction": (
            "สร้างเนื้อข่าวออกเป็น **2 แนวหลักที่ต่างโครงสร้างกันชัดเจน 100%** (รวม **4 กล่อง** ในอาร์เรย์ `articles` สำหรับโหมดปกติ+บิด) โดย **ไม่จำกัดโควตาจำนวนคำตายตัว** (ให้ความยาวสั้นหรือยาวตามกลไกและปริมาณข้อเท็จจริงจริงของข่าวประเด็นนั้นๆ เพื่อให้สกิลทำงานได้เต็มประสิทธิภาพ 100%):\n"
            "    - **แนวที่ 1 (`style_id: 'toon_dna_121'`) — ⭐ โหมด 3 ย่อหน้ากระชับเปี่ยมคุณค่า (`3-Paragraph Concise Deep-Value Standard` • `1+1+1` โมเดล จิ๊ก เนาวรัตน์ & พี่ตูน 1,600M • จับคู่ 254 DNA • 98/100):** โครงสร้าง **3 ย่อหน้าเปี่ยมคุณค่า (`1+1+1` ไม่ล็อกจำนวนคำตายตัว ยืด-หดตามเนื้อหาจริงของประเด็นนั้น)**:\n"
            "      • **ย่อหน้า 1:** ฮุคเข้าใจจบทั้งข่าวในประโยคเดียว (ผสานท่าเปิดจากการ์ด 254 แนว + `[จุดเริ่มต้น/ความต่างบทบาท] + [ใคร] + [การกระทำ/ตัวเลขผลลัพธ์จริง]`)\n"
            "      • **ย่อหน้า 2:** เจาะลึกจุดเริ่มต้น + ดีเทลวิธีปฏิบัติที่เห็นหัวใจแบบกระชับแต่ครบถ้วน (ห้ามบอกแค่ไปทำอะไรผิวเผิน! เช่น ต้องใส่ดีเทลยกมือไหว้ขออนุญาต ทำความสะอาด หวีผม แต่งหน้าด้วยแป้งกับลิปสติกที่เตรียมมาเอง พร้อมพูดอวยพรด้วยความเคารพ) โดยยึดกฎ `Single-Core Purity` (1 ข่าว = 1 แกนอารมณ์บริสุทธิ์ ห้ามเอาเรื่องคนละโทนมายำปนกัน)\n"
            "      • **ย่อหน้า 3:** ขมวดจบด้วยคุณค่าทางใจหรือมุมคิดจริงของตัวละคร (เช่น มองผู้ล่วงลับทุกท่านเป็นครูคนสำคัญที่สอนสัจธรรมและลดความยึดติดในชื่อเสียง)\n"
            "    - **แนวที่ 2 (`style_id: 'applied_hybrid_131'`) — 💡 สกิลลูกเล่นประยุกต์ข้อมูล + ไฮบริดแตกบรรทัด (98/100):** โครงสร้างแตกบรรทัด 3 บล็อก (`บล็อก 1 เปิดเรื่องด้วยลูกเล่นคมๆ` + `บล็อก 2 แตกบรรทัดเจาะลึกรายจุด สั้นหรือยาวตามจำนวนข้อเท็จจริงและกลไกของข่าวนั้นๆ` + `บล็อก 3 ขมวดจบตามธรรมชาติ`) ที่เปลี่ยนข้อมูลรีเสิร์ชลึกเป็นลูกเล่นการเล่า (หักมุมตัวเลข / แกะรอยจากของ 1 ชิ้น / ถอดรหัสคำพูดสั้นๆ / ตัดสลับ 2 โลก / กฎเหล็กจากแผลในอดีต)"
        ),
    },
    "toon_dna_121": {
        "id": "toon_dna_121",
        "short_tag": "⭐ แนวที่ 1: 3 ย่อหน้าเปี่ยมคุณค่า (1+1+1 • 254 DNA • 98/100)",
        "full_badge": "[แนวที่ 1: 3-Paragraph Concise Deep-Value Standard (1+1+1 โมเดล จิ๊ก เนาวรัตน์ + จับคู่คลัง 254 DNA Prompts + Single-Core Purity • 98/100)]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\viral-news-digest\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[แนวที่ 1: 3 ย่อหน้ากระชับเปี่ยมคุณค่า มาตรฐาน 97–98/100 (`3-Paragraph Concise Deep-Value Standard` — 1+1+1 โมเดล จิ๊ก เนาวรัตน์ + จับคู่คลัง 254 DNA Prompts + Single-Core Purity)]** (ไม่จำกัดจำนวนคำตายตัว ให้ความยาวสั้นหรือยาวตามกลไกและข้อเท็จจริงของประเด็นนั้นๆ):\n"
            "    - **ย่อหน้าที่ 1 (ฮุคเข้าใจจบทั้งข่าวในประโยคเดียว):** ผสานท่าเปิดของการ์ดจากคลัง 254 DNA Prompts เข้ากับ `[จุดเริ่มต้น/ความต่างบทบาท] + [ใคร] + [การกระทำ/ตัวเลขผลลัพธ์จริง]` เพื่อให้คนอ่านเข้าใจจบทั้งเรื่องทันที\n"
            "    - **ย่อหน้าที่ 2 (เจาะลึกจุดเริ่มต้น + ดีเทลวิธีปฏิบัติที่เห็นหัวใจ):** ร้อยจุดเริ่มต้นจริงเข้ากับรายละเอียดวิธีปฏิบัติที่สะท้อนความใส่ใจของตัวละครอย่างครบถ้วน (ห้ามบอกแค่ไปทำอะไรผิวเผิน) ยึดกฎ `Single-Core Purity` ห้ามเอาเรื่องคนละอารมณ์มาปนในข่าวเดียวเด็ดขาด\n"
            "    - **ย่อหน้าที่ 3 (ขมวดจบด้วยคุณค่าทางใจหรือมุมคิดจริงของตัวละคร):** ขมวดจบด้วยคุณค่าทางใจ วิธีคิดจริง หรือสิ่งที่ตัวละครได้รับกลับคืนมาในจิตใจ พร้อมระบุการ์ด Prompt (`style_meta`) และลิงก์ต้นทางฉบับจริง (`source_url`)"
        ),
    },
    "applied_hybrid_131": {
        "id": "applied_hybrid_131",
        "short_tag": "💡 แนวที่ 2: สกิลลูกเล่น + ไฮบริดแตกบรรทัด",
        "full_badge": "[แนวที่ 2: สกิลลูกเล่นประยุกต์ข้อมูล + ไฮบริดแตกบรรทัด (💡⭐ • รีเสิร์ชลึกแต่เล่าด้วยลูกเล่น • 98/100)]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-6-applied-hook\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[แนวที่ 2: สกิลลูกเล่นประยุกต์ข้อมูล + ไฮบริดแตกบรรทัด (💡⭐)]** (ไม่จำกัดจำนวนคำตายตัว ให้จำนวนบรรทัดและความยาวสั้นหรือยาวตามกลไกของข่าวประเด็นนั้นๆ):\n"
            "    - เปลี่ยนข้อมูลรีเสิร์ชเชิงลึกให้เป็น 'ลูกเล่นของประเด็น (Applied Narrative Hook)' ห้ามยกข้อมูลมาเรียงต่อกันทื่อๆ แบบรายงาน\n"
            "    - เลือก 1 ลูกเล่นที่เข้ากับข่าวที่สุด (ลูกเล่นที่ 1: หักมุมตัวเลขคู่เทียบ / ลูกเล่นที่ 2: แกะรอยจากของ 1 ชิ้น / ลูกเล่นที่ 3: ถอดรหัสคำพูดสั้นๆ / ลูกเล่นที่ 4: ภาพตัดสลับ 2 โลก / ลูกเล่นที่ 5: กฎเหล็กจากแผลในอดีต) ร้อยเรียงในโครงสร้างแตกบรรทัด 3 บล็อก"
        ),
    },
    "skill_set_1": {
        "id": "skill_set_1",
        "short_tag": "แบบที่ 1: อัดดีเทลตัวเลขครบ",
        "full_badge": "[ชุดสกิลที่ 1: แบบที่ 1 อัดดีเทลตัวเลข-เหตุการณ์จริงครบทุกจุด ไร้สำนวน 100%]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-1-classic-timeline\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[แบบที่ 1: อัดดีเทลตัวเลข-เหตุการณ์จริงครบทุกจุด ตัดคำโวหารทิ้ง 100%]** (ไม่จำกัดจำนวนคำตายตัว ให้ความยาวตามข้อมูลจริงของข่าว):\n"
            "    - ล้างสำนวนเปรียบเปรยทิ้ง 100% ใช้พื้นที่ไปกับ สถานที่จริง (อำเภอ/จังหวัด) + วิธีเอาตัวรอดจริง + ตัวเลขเงิน/ค่าแรง + ชื่อผลงานจุดเปลี่ยน + ที่ดิน/ทรัพย์สินปัจจุบัน\n"
            "    - มุมปกติ (Room 1): ไล่ข้อมูลจริงตั้งแต่จุดต่ำสุดจนสร้างฐานะดูแลครอบครัวสำเร็จ\n"
            "    - มุมบิดประเด็น (Room 2): เจาะสิ่งของ/สถานที่/พฤติกรรมเฉพาะ 1 อย่างในปัจจุบัน แล้วผูกกับเหตุการณ์จริงในอดีตที่เป็นต้นเหตุโดยตรง"
        ),
    },
    "skill_set_2": {
        "id": "skill_set_2",
        "short_tag": "แบบที่ 2: เปิดคำพูดจริง+แก้ปัญหา",
        "full_badge": "[ชุดสกิลที่ 2: แบบที่ 2 เปิดด้วยคำพูด/ฉากจำจริง + ไล่สเต็ปแก้ปัญหาด้วยข้อมูลล้วน]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-2-legal-forensic\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[แบบที่ 2: เปิดด้วยคำพูด/ฉากจำจริง + ไล่สเต็ปแก้ปัญหาด้วยข้อมูลล้วน]** (ไม่จำกัดจำนวนคำตายตัว ให้ความยาวตามข้อมูลจริงของข่าว):\n"
            "    - เปิดบรรทัดแรกด้วยคำพูดจริงของคนในข่าวหรือคำสัญญาในเหตุการณ์จริง แล้วไล่สเต็ปการลงมือแก้ปัญหาทีละเปลาะด้วยข้อมูลและตัวเลขจริง\n"
            "    - มุมปกติ (Room 1): เปิดด้วยคำพูด/โจทย์หลักของครอบครัว แล้วไล่สเต็ปการสร้างตัวจนปลดหนี้สำเร็จ\n"
            "    - มุมบิดประเด็น (Room 2): เปิดด้วยคำสัญญาหรือหลักคิดเฉพาะตัวในอีกห้องข้อมูลหนึ่ง แล้วคลี่คลายด้วยหลักฐานการลงมือทำจริง"
        ),
    },
    "skill_set_3": {
        "id": "skill_set_3",
        "short_tag": "แบบที่ 3: ซอยจุดย่อยเก็บข้อมูลลึก",
        "full_badge": "[ชุดสกิลที่ 3: แบบที่ 3 ซอยช่วงกลางสั้นกระชับ เก็บข้อมูลลึกครบทุกจุด]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-3-fact-collision\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[แบบที่ 3: ซอยช่วงกลางสั้นกระชับ เก็บข้อมูลลึกครบทุกจุด]** (ไม่จำกัดจำนวนคำตายตัว ให้ความยาวตามข้อมูลจริงของข่าว):\n"
            "    - ซอยช่วงกลางเป็นบรรทัดย่อยเพื่ออัดข้อเท็จจริงลึกได้ครบทุกจุดในโพสต์เดียวโดยไม่อึดอัดและไร้สำนวนฟุ่มเฟือย\n"
            "    - มุมปกติ (Room 1) และ มุมบิดประเด็น (Room 2) แยกห้องข้อมูลกัน 100%"
        ),
    },
    "skill_set_4": {
        "id": "skill_set_4",
        "short_tag": "แบบที่ 4: หมัดหนักเทียบตัวเลข",
        "full_badge": "[ชุดสกิลที่ 4: แบบที่ 4 หมัดหนักคู่เทียบตัวเลขก่อน-หลัง]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-4-micro-quote-decoder\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[แบบที่ 4: หมัดหนักคู่เทียบตัวเลขก่อน-หลัง]** (ไม่จำกัดจำนวนคำตายตัว ให้ความยาวตามข้อมูลจริงของข่าว):\n"
            "    - เปิดด้วยการวางคู่เทียบตัวเลขหรือสถานะก่อน-หลังที่ตัดกันแรงในประโยคเดียว\n"
            "    - ช่วงกลางขยายจุดต่ำสุดในอดีตและจุดเปลี่ยนผลลัพธ์จริง แล้วตบจบอย่างคมชัด"
        ),
    },
    "skill_set_5": {
        "id": "skill_set_5",
        "short_tag": "แบบที่ 5: ไฮบริดเล่าลื่นข้อมูลแน่น (⭐)",
        "full_badge": "[ชุดสกิลที่ 5: แบบที่ 5 (⭐) ไฮบริดเล่าลื่น ข้อมูลไวรัลแน่นสุดซอย 100/100]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-5-ultimate-3d-hybrid\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[แบบที่ 5 (⭐): ไฮบริดเล่าลื่น ข้อมูลไวรัลแน่นสุดซอย 100/100]** (ไม่จำกัดจำนวนคำตายตัว ให้ความยาวตามข้อมูลจริงของข่าว):\n"
            "    - ไร้สำนวนเพ้อ 100% แต่ร้อยเรียงฉากชีวิตจริง ตัวเลข และเหตุผลให้ไหลลื่นเหมือนนั่งฟังเจ้าตัวเล่าเอง\n"
            "    - อัดดีเทลครบทุกมิติที่มีข้อมูลจริง (สถานที่ + วิธีเอาตัวรอด + ตัวเลขเงิน/ค่าตัว + ชื่อผลงานจุดเปลี่ยน + ทรัพย์สินปัจจุบัน + เหตุผลการใช้งานจริงเพื่อครอบครัว)"
        ),
    },
    "skill_set_6": {
        "id": "skill_set_6",
        "short_tag": "แบบที่ 6: ลูกเล่นประยุกต์ข้อมูล (💡)",
        "full_badge": "[ชุดสกิลที่ 6: สกิลลูกเล่นประยุกต์ข้อมูล (💡) รีเสิร์ชลึกแต่เล่าด้วยลูกเล่น]",
        "skill_path": r"C:\Users\User\.gemini\config\skills\news-skill-set-6-applied-hook\SKILL.md",
        "method_instruction": (
            "ใช้เฉพาะหลักการของ **[ชุดสกิลที่ 6 (💡): สกิลลูกเล่นประยุกต์ข้อมูล — รีเสิร์ชลึกแต่เล่าด้วยลูกเล่น]** (ไม่จำกัดจำนวนคำตายตัว ให้ความยาวสั้นหรือยาวตามกลไกของข่าวประเด็นนั้นๆ):\n"
            "    - เปลี่ยนข้อมูลรีเสิร์ชเชิงลึกให้เป็น 'ลูกเล่นของประเด็น (Applied Narrative Hook)' ห้ามยกข้อมูลมาเรียงต่อกันทื่อๆ แบบรายงาน\n"
            "    - เลือก 1 ลูกเล่นต่อ 1 กล่อง (ลูกเล่นที่ 1: หักมุมตัวเลข / ลูกเล่นที่ 2: แกะรอยจากของ 1 ชิ้น / ลูกเล่นที่ 3: ถอดรหัสคำพูดสั้นๆ / ลูกเล่นที่ 4: ภาพตัดสลับ 2 โลก / ลูกเล่นที่ 5: กฎเหล็กที่ตั้งขึ้นเพราะแผลในอดีต)"
        ),
    },
}


VALID_STYLE_IDS = (
    "toon_dna_121",
    "applied_hybrid_131",
    "skill_set_1",
    "skill_set_2",
    "skill_set_3",
    "skill_set_4",
    "skill_set_5",
    "skill_set_6",
)


def get_skill_set_info(skill_set: str) -> Dict[str, str]:
    if skill_set == "dual_core_styles":
        return SKILL_SET_REGISTRY["all_5_styles"]
    return SKILL_SET_REGISTRY.get(skill_set) or SKILL_SET_REGISTRY["all_5_styles"]


def _normalize_result_articles(result_data: Dict[str, Any], output_mode: str, skill_set: str = "all_5_styles") -> Dict[str, Any]:
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
                item_skill_id = str(item.get("style_id") or item.get("skill_set") or "").strip()
                if item_skill_id not in VALID_STYLE_IDS:
                    title_check = str(item.get("title") or item.get("label") or "")
                    if any(k in title_check for k in ("1+2+1", "แนวที่ 1", "พี่ตูน", "254")):
                        item_skill_id = "toon_dna_121"
                    elif any(k in title_check for k in ("แนวที่ 2", "ลูกเล่น", "1+3+1")):
                        item_skill_id = "applied_hybrid_131"
                    else:
                        for snum in ("1", "2", "3", "4", "5", "6"):
                            if f"แบบที่ {snum}" in title_check or f"ชุดสกิลที่ {snum}" in title_check:
                                item_skill_id = f"skill_set_{snum}"
                                break
                    if not item_skill_id:
                        if s_info["id"] in VALID_STYLE_IDS:
                            item_skill_id = s_info["id"]
                        elif len(raw_articles) == 4:
                            item_skill_id = "toon_dna_121" if idx < 2 else "applied_hybrid_131"
                        elif len(raw_articles) == 2 and output_mode in ("standard_4lock", "spin_only_1", "strict_clip_only"):
                            item_skill_id = "toon_dna_121" if idx == 0 else "applied_hybrid_131"
                        elif len(raw_articles) >= 10:
                            item_skill_id = f"skill_set_{min(6, (idx // 2) + 1)}"
                        elif len(raw_articles) in (5, 6):
                            item_skill_id = f"skill_set_{idx + 1}"
                        else:
                            item_skill_id = "toon_dna_121" if output_mode == "toon_121" else "applied_hybrid_131"

                item_sinfo = get_skill_set_info(item_skill_id)
                label = str(item.get("label") or item.get("mode_badge") or "").strip()
                title = str(item.get("title") or "").strip()
                angle_type = str(item.get("angle_type") or "").strip()
                if not angle_type:
                    angle_type = "spin" if ("บิด" in label or "บิด" in title) else "normal"
                if not label:
                    if angle_type == "spin" or (idx % 2 == 1 and output_mode in ("normal_1_spin_1", "normal_1_spin_2")) or output_mode == "spin_only_1":
                        label = f"{item_sinfo['short_tag']} • มุมบิดประเด็น"
                        angle_type = "spin"
                    elif output_mode == "toon_121":
                        label = f"{item_sinfo['short_tag']} • โหมด 1+2+1 (98/100)"
                    elif output_mode == "viral_3_para":
                        label = f"{item_sinfo['short_tag']} • ข่าวสั้น 3 ย่อหน้า (254 DNA)"
                    elif output_mode == "strict_clip_only":
                        label = f"{item_sinfo['short_tag']} • เฉพาะในคลิป 100%"
                    else:
                        label = f"{item_sinfo['short_tag']} • มุมปกติ"
                if not title:
                    title = f"{item_sinfo['short_tag']} — {'มุมบิดประเด็น' if angle_type == 'spin' else 'มุมปกติ'}"
                normalized.append({
                    "label": label,
                    "title": title,
                    "style_id": item_sinfo["id"],
                    "angle_type": angle_type,
                    "style_meta": str(item.get("style_meta") or "").strip(),
                    "source_url": str(item.get("source_url") or "").strip(),
                    "skill_set": item_sinfo["id"],
                    "skill_set_short": item_sinfo["short_tag"],
                    "skill_set_label": item_sinfo["full_badge"],
                    "content": content,
                })

    # Also check if result_data has explicit style_variants array
    raw_variants = result_data.get("style_variants")
    if isinstance(raw_variants, list) and not normalized:
        for v in raw_variants:
            if not isinstance(v, dict):
                continue
            sid = str(v.get("style_id") or "toon_dna_121")
            v_info = get_skill_set_info(sid)
            n_txt = _clean_article_box_text(v.get("normal_content") or "")
            s_txt = _clean_article_box_text(v.get("spin_content") or "")
            if n_txt:
                normalized.append({
                    "label": f"{v_info['short_tag']} • มุมปกติ",
                    "title": f"{v_info['short_tag']} — มุมปกติ",
                    "style_id": v_info["id"],
                    "angle_type": "normal",
                    "skill_set": v_info["id"],
                    "skill_set_short": v_info["short_tag"],
                    "skill_set_label": v_info["full_badge"],
                    "content": n_txt,
                })
            if s_txt:
                normalized.append({
                    "label": f"{v_info['short_tag']} • มุมบิดประเด็น",
                    "title": f"{v_info['short_tag']} — มุมบิดประเด็น",
                    "style_id": v_info["id"],
                    "angle_type": "spin",
                    "skill_set": v_info["id"],
                    "skill_set_short": v_info["short_tag"],
                    "skill_set_label": v_info["full_badge"],
                    "content": s_txt,
                })

    cleaned_raw = _clean_article_box_text(result_data.get("raw_news") or "")

    if not normalized and cleaned_raw:
        default_label = (
            "บิดประเด็น" if output_mode == "spin_only_1"
            else "โหมด 1+2+1 (พี่ตูน 1,600M)" if output_mode == "toon_121"
            else "ข่าวสั้น 3 ย่อหน้า (254 DNA)" if output_mode == "viral_3_para"
            else "เฉพาะในคลิป 100%" if output_mode == "strict_clip_only"
            else "มุมปกติ"
        )
        normalized.append({
            "label": default_label,
            "title": result_data.get("headline") or f"1. ข่าวแบบ{default_label}",
            "style_id": "toon_dna_121" if output_mode == "toon_121" else "applied_hybrid_131",
            "angle_type": "spin" if output_mode == "spin_only_1" else "normal",
            "skill_set": s_info["id"],
            "skill_set_short": short_tag,
            "skill_set_label": full_badge,
            "content": cleaned_raw,
        })

    # Build structured style_variants for instant Tab switching on the Web UI
    style_variants: List[Dict[str, Any]] = []
    for s_idx, sid in enumerate(VALID_STYLE_IDS, 1):
        st_info = get_skill_set_info(sid)
        matching = [a for a in normalized if a.get("style_id") == sid]
        if matching:
            normals = [a for a in matching if a.get("angle_type") != "spin"]
            spins = [a for a in matching if a.get("angle_type") == "spin"]
            style_variants.append({
                "style_id": sid,
                "style_no": s_idx,
                "style_short": st_info["short_tag"],
                "style_badge": st_info["full_badge"],
                "normal_title": normals[0]["title"] if normals else f"{st_info['short_tag']} — มุมปกติ",
                "normal_content": normals[0]["content"] if normals else "",
                "spin_title": spins[0]["title"] if spins else f"{st_info['short_tag']} — มุมบิดประเด็น",
                "spin_content": spins[0]["content"] if spins else "",
                "boxes": matching,
            })

    result_data["style_variants"] = style_variants
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


def _run_agy_sync_with_watcher(cmd: List[str], cwd: Path, result_json_path: Path, timeout: int = 480) -> subprocess.CompletedProcess:
    """Run agy.exe and watch for result.json creation so we return immediately once write_file completes."""
    import time
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    start_ts = time.time()
    last_stable_check = 0.0
    while True:
        ret = proc.poll()
        if ret is not None:
            out, err = proc.communicate()
            return subprocess.CompletedProcess(cmd, ret, out, err)

        now = time.time()
        elapsed = now - start_ts
        if elapsed > timeout:
            try:
                proc.kill()
            except Exception:
                pass
            raise subprocess.TimeoutExpired(cmd, timeout)

        if now - last_stable_check >= 1.2 and result_json_path.exists():
            last_stable_check = now
            try:
                if result_json_path.stat().st_size > 200:
                    time.sleep(0.6)
                    with open(result_json_path, "r", encoding="utf-8") as f:
                        parsed = json.load(f)
                    if isinstance(parsed, dict) and (parsed.get("articles") or parsed.get("raw_news")):
                        try:
                            proc.terminate()
                            proc.wait(timeout=3)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                        return subprocess.CompletedProcess(cmd, 0, "SAVED_OK", "")
            except Exception:
                pass

        time.sleep(0.5)


async def run_agy_with_result_watcher(cmd: List[str], cwd: Path, result_json_path: Path, timeout: int = 480) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(_run_agy_sync_with_watcher, cmd, cwd, result_json_path, timeout)


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

    # Load Skill Guides into SKILL_GUIDES.md (kept compact < 35KB so agy.exe reads it in 1 single read_file call without truncation)
    writer_skill_path = Path(r"C:\Users\User\.gemini\config\skills\viral-news-writer\SKILL.md")
    digest_skill_path = Path(r"C:\Users\User\.gemini\config\skills\viral-news-digest\SKILL.md")
    spin_skill_path = Path(r"C:\Users\User\.gemini\config\skills\viral-news-spin\SKILL.md")
    prompt_lib_path = Path(r"C:\Users\User\.gemini\config\skills\viral-prompt-library\SKILL.md")
    guides_parts: List[str] = []

    # Pre-match Top 6 DNA Prompt Cards from 254 Library using job context + focus_instruction
    match_blob_parts = [raw_text or "", focus_instruction or ""]
    for v_item in extracted_meta.get("videos", []):
        match_blob_parts.append(str(v_item.get("title") or ""))
        match_blob_parts.append(str(v_item.get("description") or "")[:600])
    for a_item in extracted_meta.get("articles", []):
        match_blob_parts.append(str(a_item.get("title") or ""))
        match_blob_parts.append(str(a_item.get("description") or "")[:600])
        match_blob_parts.append(str(a_item.get("body_text") or "")[:800])
    matched_dna_section = _get_top_matched_254_prompts(" ".join(match_blob_parts), top_k=6)
    if matched_dna_section:
        guides_parts.append(matched_dna_section)

    if s_info["id"] == "all_5_styles":
        summary_lines = [
            "=== คู่มือ 2 แนวหลัก มาตรฐาน 97–98/100 (ใช้ข้อเท็จจริงชุดเดียวกัน จัด 2 โครงสร้างที่ต่างกันชัดเจน) ===",
            f"\n### 1. {SKILL_SET_REGISTRY['toon_dna_121']['full_badge']}\n{SKILL_SET_REGISTRY['toon_dna_121']['method_instruction']}",
            f"\n### 2. {SKILL_SET_REGISTRY['applied_hybrid_131']['full_badge']}\n{SKILL_SET_REGISTRY['applied_hybrid_131']['method_instruction']}",
        ]
        guides_parts.append("\n".join(summary_lines))
        sp6 = Path(SKILL_SET_REGISTRY["skill_set_6"]["skill_path"])
        if sp6.exists():
            guides_parts.append("=== คู่มือละเอียด แนวที่ 2: สกิลลูกเล่นประยุกต์ข้อมูล (1+3+1) ===\n" + sp6.read_text(encoding="utf-8")[:6500])
    else:
        selected_skill_path = Path(s_info["skill_path"])
        if selected_skill_path.exists():
            guides_parts.append(
                f"=== คู่มือชุดสกิลที่เลือกใช้งาน: {s_info['full_badge']} ===\n"
                + selected_skill_path.read_text(encoding="utf-8")[:12000]
            )

    if digest_skill_path.exists():
        digest_txt = digest_skill_path.read_text(encoding="utf-8")
        guides_parts.append("=== คู่มือสกิล `viral-news-digest` (รวมโหมดกระชับ 1+2+1 โมเดลพี่ตูน 1,600M & มาตรฐาน 97-98/100) ===\n" + digest_txt[:14000])
    if spin_skill_path.exists():
        spin_txt = spin_skill_path.read_text(encoding="utf-8")
        guides_parts.append("=== คู่มือสกิล `viral-news-spin` (รวมสูตรที่ 9 ขุดความดีเงียบๆ สวนกระแสดราม่า) ===\n" + spin_txt[:10000])
    if output_mode == "viral_3_para":
        if writer_skill_path.exists():
            guides_parts.append("=== คู่มือสกิล `viral-news-writer` ===\n" + writer_skill_path.read_text(encoding="utf-8")[:6000])
        if prompt_lib_path.exists():
            guides_parts.append("=== คู่มือคลัง 254 Prompts (`viral-prompt-library`) ===\n" + prompt_lib_path.read_text(encoding="utf-8")[:6000])

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
        f"  • โหมดสกิลที่ใช้ในงานนี้: **{s_info['full_badge']}**\n"
        f"  • หลักการเฉพาะ:\n{s_info['method_instruction']}\n"
        "  • 🛡️ กฎเหล็กความแม่นยำสูงสุด (Strict Fact-Only & Zero Hallucination 100% — ห้ามมั่วหรือเติมแต่งข้อมูลเด็ดขาด):\n"
        "    - **ทำจากข้อมูลจริงที่มีหลักฐานรองรับ 100% เท่านั้น:** ทุกชื่อบุคคล ตัวเลขเงิน จำนวนปี สถานที่ และคำพูด ต้องมีที่มาจากคลิป/ภาพ/ข้อความต้นทาง หรือผลลัพธ์ `search_web` ที่ยืนยันตรงเคสจริงเท่านั้น ห้ามเดา ห้ามกะประมาณตัวเลขเอง และห้ามจำสลับกับบทละคร/ซิตคอมเด็ดขาด!\n"
        "    - **กฎเหล็ก Single-Core Purity (1 ข่าว = 1 แกนอารมณ์บริสุทธิ์ ห้ามยัดข้อมูลหลุดโทนมาปนในข่าวเดียว):** ห้ามเอา 2 ประเด็นคนละอารมณ์ของคนเดียวกันมายำปนในกล่องข่าวเดียวกันเด็ดขาด (เช่น ห้ามเอาเรื่องเปิดร้านอาหารมาปนกับเรื่องวิ่งหาเงินซื้อเครื่องช่วยหายใจให้ห้องไอซียู หรือห้ามเอาเรื่องธุรกิจรีสอร์ทมาปนกับเรื่องเด็กวัดสู้ชีวิตตามคำสอนพ่อ) ต้องล็อกแกนเรื่องเดียวที่คมที่สุดร้อยตั้งแต่ต้นจนจบให้เป็นเนื้อเดียวกัน 100%\n"
        "    - **หลักการ 2 แนวหลัก (Same Verified Facts, 2 Distinct Master Styles):** ทั้ง 2 แนวหลัก (`toon_dna_121` และ `applied_hybrid_131`) คือการนำ **ข้อเท็จจริงจริงชุดเดียวกันที่ตรวจสอบแล้ว** มาขัดเกลาซ้ำหลายรอบใน 2 โครงสร้างที่ต่างกันชัดเจน (แนวที่ 1: โหมด 3 ย่อหน้ากระชับเปี่ยมคุณค่า 1+1+1 โมเดล จิ๊ก เนาวรัตน์ จับคู่ 254 DNA ฮุคจบย่อหน้าแรก+ลงลึกวิธีปฏิบัติ+ปิดด้วยคุณค่าทางใจ / แนวที่ 2: สกิลลูกเล่นประยุกต์ข้อมูล + ไฮบริดแตกบรรทัด 1+3+1) จนได้ความคมระดับ `97–98/100` ทุกกล่อง!\n"
        "    - **กรณีข่าวมีข้อมูลน้อย / คลิปสั้น (Fact-Bounded Length Adaptation — มีแค่ไหนทำแค่นั้น):** ให้ใส่ **เฉพาะจุดที่มีข้อมูลจริงปรากฏในต้นทางหรือค้นเจอจากแหล่งข่าวที่เชื่อถือได้เท่านั้น** หากข่าวนั้นไม่มีข้อมูลบางจุด **ให้ตัดจุดที่ไม่มีข้อมูลออกและปรับลดจำนวนบรรทัด/ความยาวคำลงตามข้อเท็จจริงที่มีอยู่จริงทันที** ห้ามปั้นน้ำหรือเขียนขยายความลอยๆ เด็ดขาด!\n"
        "    - **กรณีข้อมูลไม่พอทำมุมบิดประเด็นห้องใหม่ (Fallback for Low-Context Clips):** หากค้น `search_web` เต็มที่แล้วเป็นคลิปชาวบ้าน/CCTV/เหตุการณ์เดี่ยวที่ไม่มีข้อมูลภูมิหลังหรือเรื่องราวอีกห้องหนึ่งบนเว็บเลย ห้ามมโนประวัติชีวิตเองเด็ดขาด! ให้ใช้มุมบิดประเด็นเป็นการ **เจาะดีเทลจุดสังเกตจริงในคลิป/คำพูดจริงอีกแง่มุมหนึ่ง หรือข้อกฎหมาย/ข้อเท็จจริงมาตรฐานที่ตรวจสอบได้จริง** เท่าที่มีหลักฐานรองรับเท่านั้น\n"
        "    - **สูตรที่ 9 สำหรับบิดข่าวน้ำดี/ขุดความดีเงียบๆ สวนกระแสดราม่า (`Positive Counter-Current & Side-Character Good Deed Spin`):** หากเป็นข่าวคนดังหรือตัวละครในข่าวใหญ่ สามารถค้นขุด 'ความดีเงียบๆ ที่คนยังไม่ค่อยรู้' (เช่น สู้ชีวิตจากศูนย์, ความกตัญญูดูแลพ่อแม่ป่วยติดเตียงด้วยตัวเอง, รวยร้อยล้านแต่ติดดิน, จิตอาสาช่วยเหลือสังคมเงียบๆ) มาบิดประเด็นมุมบวกกินใจได้\n"
        "  • 🏆 มาตรฐานการขัดเกลา 97–98/100 (`97–98/100 Multi-Round Polish Standard` + `254-Prompt DNA Matching`):\n"
        "    - **คัดกรองโทนบวกอบอุ่นใจไร้ดราม่า & AI Content Safety Filter 100% (`Positive Feel-Good Filter`):** เมื่อทำข่าวน้ำดี/ความกตัญญู/สู้ชีวิต ให้เกลาคำหนักๆ หรือคำดราม่า ('คำดูถูก', 'คำครหา', 'วินาทีเฉียดตาย', 'ถอดหัวโขน') ออกให้เป็นโทนบวกอบอุ่นใจ และห้ามใช้คำว่า 'ศพ / ผู้เสียชีวิต / ห้องดับจิต' ให้ใช้คำว่า 'ผู้ล่วงลับ / ผู้วายชนม์ / ห้องสุดท้าย' แทนเสมอเพื่อไม่ให้ชน AI Safety Filter\n"
        "    - **จับคู่คลัง 254 DNA Prompts (`Mandatory 254-Prompt DNA Matching`):** ห้ามขึ้นต้นเป็นแพตเทิร์นเดียวกันซ้ำๆ (`[ชื่อ] [อาชีพ] ทำ...`) ให้เลือกการ์ดที่ตรงเนื้อหาที่สุดจากหมวด `🧬 การ์ด DNA แนะนำจากคลัง 254 Prompts` ใน `SKILL_GUIDES.md` ผสานท่าเปิดเฉพาะตัวของการ์ดเข้ากับ `[จุดเริ่มต้น/เหตุผลสำคัญ] + [ใคร] + [การกระทำ/ตัวเลขผลลัพธ์จริง]` และขัดเกลาซ้ำหลายรอบจนได้ระดับ `97–98/100` พร้อมระบุรหัสการ์ดและคะแนนคุณภาพไว้ใน `style_meta` และระบุลิงก์ต้นทางฉบับจริง (`Direct Source URL`) ไว้ใน `source_url` ของทุกกล่อง\n"
        "    - ตัดคำโวหารและสำนวนเปรียบเปรยออก 100% (ห้ามเขียน 'ซ่อนความทรงจำไว้ทุกดวง', 'คือมื้อประทังชีวิต', 'เติมเต็มความฝัน') ใช้โควตาคำไปกับข้อเท็จจริงที่มีหลักฐานจริงเท่านั้น\n"
        "    - แบนคำแม่พิมพ์นักวิเคราะห์ซ้ำซาก 100%: ห้ามเปิดเรื่องด้วย 'เบื้องหลัง...', 'เหตุผลที่... มีที่มาจาก...', 'เส้นทางพลิกผันของ...', 'ท่ามกลางกระแส...' ห้ามขึ้นกลางเรื่องด้วย 'หากย้อนกลับไป...', 'จุดเริ่มต้นมาจาก...', 'เหตุการณ์นี้เริ่มต้นขึ้นเมื่อ...', 'ในทางธุรกิจ...' และห้ามจบเทศนาสั่งสอน ('จึงสะท้อนชัดเจนว่า...', 'สิ่งนี้สะท้อน...', 'จึงเป็นบทเรียนชีวิตจริงว่า...', 'จึงเป็นเครื่องพิสูจน์ว่า...', 'นี่แหละ...', 'จาก... กลายเป็น...')\n"
        "    - กฎแยกห้องข้อมูล (`Strict Room Separation`): เมื่อมีข้อมูลชั้นที่ 2 จาก `search_web` เพียงพอ ให้แยกเรื่องราวระหว่าง `มุมปกติ (Room 1)` กับ `มุมบิดประเด็น (Room 2)` ไม่ให้ซ้ำฉากกัน\n"
        "    - **ปลดล็อกโควตาจำกัดคำตายตัว 100% (`Fact-Driven Natural Length — สั้นหรือยาวตามกลไกของข่าวประเด็นนั้นๆ`):** ห้ามจำกัดจำนวนคำขั้นต่ำหรือขั้นสูงต่อข่าว! ข่าวประเด็นสั้นก็เขียนสั้นคมกระชับ ข่าวที่มีจุดเริ่มต้น กลไกเหตุผล ดีเทลวิธีปฏิบัติ หรือข้อกฎหมายสำคัญหลายชั้นก็ให้กางรายละเอียดได้เต็มที่ครบถ้วนตามเนื้อผ้าจริงของข่าวนั้น (หากย่อหน้าไหนข้อมูลแน่นให้เคาะบรรทัดตามจังหวะเหตุการณ์ให้อ่านลื่นบนมือถือ โดยห้ามตัดทอนเนื้อหาสำคัญออก)\n"
        "    - กฎ 95/5 & Pre-Generator Filter & Meta Safety 100%: ห้ามใส่รหัสเทป EP.xxx หรือวันที่ราชการแข็งๆ ตัดคำอังกฤษปนไทยในเนื้อข่าวออก 100% และงดถ้อยคำที่ส่อผิดกฎชุมชน Meta 100%"
    )

    if output_mode == "toon_121":
        mode_instruction = (
            "โหมดที่เลือก: **⭐ โหมดเขียนกระชับ 3 ย่อหน้าเปี่ยมคุณค่า มาตรฐาน 97–98/100 (`3-Paragraph Concise Deep-Value Standard` — 1+1+1 โมเดล จิ๊ก เนาวรัตน์ + จับคู่คลัง 254 DNA Prompts + Single-Core Purity)**\n"
            "  • ใช้โครงสร้าง 3 ย่อหน้าเปี่ยมคุณค่า (`1+1+1` ไม่จำกัดจำนวนคำตายตัว ยืด-หดตามเนื้อหาและกลไกจริงของข่าวประเด็นนั้นๆ) ตาม Section 4 ของ `viral-news-digest` คู่กับ 4 กฎเหล็กมาตรฐาน `97–98/100`:\n"
            "    - **ย่อหน้าที่ 1 (Full-Story First-Paragraph Hook + 254-Prompt DNA Opening — 1 ประโยคคมๆ):** ห้ามเกริ่นอ้อมค้อม และห้ามขึ้นต้นซ้ำแพตเทิร์น (`[ชื่อ] [อาชีพ] ทำ...`)! ต้องผสานท่าเปิดของการ์ดจากคลัง `viral-prompt-library` (254 แนว) เข้ากับ `[จุดเริ่มต้น/ความต่างบทบาท] + [ใคร] + [การกระทำ/ตัวเลขผลลัพธ์จริง]` เพื่อให้คนอ่านเข้าใจจบทั้งเรื่องได้ในย่อหน้าแรกทันที\n"
            "    - **ย่อหน้าที่ 2 (เจาะลึกจุดเริ่มต้น + ดีเทลวิธีปฏิบัติที่เห็นหัวใจแบบกระชับ — 1–2 ประโยค):** ห้ามบอกแค่ว่าไปทำอะไรผิวเผิน! ต้องร้อยจุดเริ่มต้นจริงเข้ากับรายละเอียดวิธีปฏิบัติที่สะท้อนความใส่ใจของตัวละคร (เช่น ยกมือไหว้ขออนุญาตก่อนลงมือ ทำความสะอาด หวีผม แต่งหน้าด้วยแป้งกับลิปสติกที่เตรียมมาเอง พร้อมพูดอวยพรด้วยความเคารพ) และยึดกฎ `Single-Core Purity` (ห้ามเอาเรื่องอื่นคนละอารมณ์มาปนในข่าวเดียวเด็ดขาด)\n"
            "    - **ย่อหน้าที่ 3 (ท่อนปิดท้ายด้วยคุณค่าทางใจและมุมคิดจริงของตัวละคร — 1–2 ประโยค):** ขมวดจบด้วยคุณค่าทางใจ วิธีคิดจริง หรือสิ่งที่ตัวละครได้รับกลับคืนมาในจิตใจ (เช่น มองผู้ล่วงลับทุกท่านเป็นครูคนสำคัญที่สอนสัจธรรมและลดความยึดติดในชื่อเสียง) ให้กินใจกว่ารายงานข่าวแห้งๆ (ห้ามจบด้วยคำแม่พิมพ์ 'จึงสะท้อนชัดเจนว่า...')\n"
            "  • ให้สร้างอาร์เรย์ `articles` จำนวน **2 กล่อง** (`style_id: 'toon_dna_121'`, `angle_type: 'normal'` และ `angle_type: 'spin'` ตามสูตรที่ 9 หรือมุมบิดข้อมูลจริงชั้นที่ 2) พร้อมระบุรหัสการ์ด Prompt ที่ใช้ + คะแนนคุณภาพ `98/100` ไว้ใน `style_meta` และแนบลิงก์ต้นทางฉบับจริง (`Direct Source URL`) ไว้ใน `source_url`\n"
            f"{core_rules}"
        )
    elif s_info["id"] == "all_5_styles":
        if output_mode == "strict_clip_only":
            mode_instruction = (
                "โหมดที่เลือก: **แกะเฉพาะในคลิป/โพสต์ 100% แสดงผลครบทั้ง 2 แนวหลัก (`2 แนว × 1 มุม = 2 กล่องข่าว`)**\n"
                "  • ใช้เฉพาะข้อเท็จจริงที่ได้จากภาพ-เสียงในคลิปและข้อความโพสต์ต้นทาง 100% เท่านั้น (ห้ามใช้ `search_web` ดึงข่าวอื่นมาปน และห้ามเดาข้อมูลที่ไม่มีในคลิปเด็ดขาด)\n"
                "  • นำข้อเท็จจริงจริงชุดเดียวกันนี้ มาเรียบเรียงออกเป็น **2 แนวหลัก (`style_id: 'toon_dna_121'` และ `'applied_hybrid_131'`, `angle_type: 'normal'`)** หากคลิปสั้นมีข้อมูลน้อย ให้เขียนสั้นกระชับเท่าที่มีข้อมูลจริงเท่านั้น\n"
                f"{core_rules}"
            )
        elif output_mode == "standard_4lock":
            mode_instruction = (
                "โหมดที่เลือก: **มุมปกติล้วน แสดงผลครบทั้ง 2 แนวหลัก (`2 แนว × 1 มุมปกติ = 2 กล่องข่าว`)**\n"
                "  • สรุปใจความข่าวหลักจากข้อเท็จจริงที่มีหลักฐานรองรับ 100% แล้วจัดวางโครงสร้างออกเป็น **2 แนวหลัก (`style_id: 'toon_dna_121'` และ `'applied_hybrid_131'`, `angle_type: 'normal'`)** พร้อมขัดเกลาซ้ำจนได้ระดับ `97–98/100`\n"
                f"{core_rules}"
            )
        elif output_mode == "spin_only_1":
            mode_instruction = (
                "โหมดที่เลือก: **บิดประเด็นล้วน แสดงผลครบทั้ง 2 แนวหลัก (`2 แนว × 1 มุมบิดประเด็น = 2 กล่องข่าว`)**\n"
                "  • เจาะมุมบิดประเด็นจากข้อเท็จจริงจริงที่ค้นเจอและตรวจสอบยืนยันได้ 100% แล้วจัดวางโครงสร้างออกเป็น **2 แนวหลัก (`style_id: 'toon_dna_121'` และ `'applied_hybrid_131'`, `angle_type: 'spin'`)** พร้อมขัดเกลาซ้ำจนได้ระดับ `97–98/100`\n"
                f"{core_rules}"
            )
        elif output_mode == "viral_3_para":
            mode_instruction = (
                "โหมดที่เลือก: **ข่าวสั้นไวรัล 3 ย่อหน้า (จับคู่คลัง 254 DNA Prompts `viral-prompt-library` + `viral-news-writer`)**\n"
                "  • เลือกการ์ด DNA จากคลัง 254 Prompts ที่ตรงกับเนื้อข่าวที่สุด แล้วเขียนข่าว 3 ย่อหน้ากระชับตามโครงสร้างของการ์ดนั้น (สร้าง 2 กล่องใน `articles`: กล่องที่ 1 มุมปกติ 3 ย่อหน้า และ กล่องที่ 2 มุมบิดประเด็น 3 ย่อหน้า พร้อมระบุรหัส `[prompt_xxxx • 98/100]` ใน `style_meta` และ `source_url`)\n"
                f"{core_rules}"
            )
        elif output_mode == "normal_1_spin_2":
            mode_instruction = (
                "โหมดที่เลือก: **มุมปกติ 1 + บิดประเด็น 2 มุม แสดงผลครบทั้ง 2 แนวหลัก (`2 แนว × 3 มุม = 6 กล่องข่าว`)**\n"
                "  • สร้าง 3 มุม (`มุมปกติ`, `บิดประเด็น 1`, `บิดประเด็น 2`) สำหรับทั้ง 2 แนวหลัก (`style_id: 'toon_dna_121'` และ `'applied_hybrid_131'`) รวม 6 กล่อง โดยขัดเกลาซ้ำจนได้ระดับ `97–98/100`\n"
                f"{core_rules}"
            )
        else:
            mode_instruction = (
                "โหมดที่เลือก: **แสดงผลครบทั้ง 2 แนวหลัก มาตรฐาน 97–98/100 (`toon_dna_121` และ `applied_hybrid_131` = รวม 4 กล่องคมๆ) เพื่อให้พนักงานเลือกหยิบไปใช้เอง โดยยึดคำสั่งในช่องความต้องการ (`focus_instruction`) เป็นอันดับ 1**\n"
                "  • **กรณีที่ 1 (ข่าวมีข้อมูลครบทั้งมุมปกติ + มีข้อมูลจริงชั้นที่ 2 จาก `search_web` สำหรับบิดประเด็น):** ให้สร้างอาร์เรย์ `articles` จำนวน **4 กล่อง (2 แนวหลัก × 2 มุม คือ `angle_type: 'normal'` และ `angle_type: 'spin'`)** โดยทุ่มสมาธิขัดเกลาทุกกล่องซ้ำหลายรอบจนได้ระดับ **`97–98/100`**:\n"
                "    1. กล่องที่ 1 (`style_id: 'toon_dna_121'`, `angle_type: 'normal'`, `label: 'แนวที่ 1 (3 ย่อหน้า • 254 DNA) • มุมปกติ'`): โหมด 3 ย่อหน้ากระชับเปี่ยมคุณค่า (`1+1+1` โมเดล จิ๊ก เนาวรัตน์) ย่อหน้า 1 ผสานท่าเปิด 254 DNA ฮุคเข้าใจจบทั้งเรื่องในประโยคเดียว + ย่อหน้า 2 เจาะลึกจุดเริ่มต้นและวิธีปฏิบัติที่เห็นหัวใจแบบกระชับ (ยึดกฎ `Single-Core Purity`) + ย่อหน้า 3 ขมวดจบด้วยคุณค่าทางใจ/มุมคิดจริงของตัวละคร (พร้อมระบุการ์ดที่ใช้ใน `style_meta` และลิงก์ต้นทางฉบับจริงใน `source_url`)\n"
                "    2. กล่องที่ 2 (`style_id: 'toon_dna_121'`, `angle_type: 'spin'`, `label: 'แนวที่ 1 (3 ย่อหน้า • 254 DNA) • มุมบิดประเด็น'`): โหมด 3 ย่อหน้ากระชับเปี่ยมคุณค่า (`1+1+1` โมเดล จิ๊ก เนาวรัตน์) สำหรับมุมบิดประเด็น (Room 2 / สูตรที่ 9 ขุดความดีเงียบๆ หรือข้อมูลจริงชั้นที่ 2 ตามที่พนักงานรีเควส) พร้อมปิดท้ายด้วยคุณค่าทางใจ/มุมคิดจริงของตัวละคร\n"
                "    3. กล่องที่ 3 (`style_id: 'applied_hybrid_131'`, `angle_type: 'normal'`, `label: 'แนวที่ 2 (ลูกเล่น 1+3+1) • มุมปกติ'`): สกิลลูกเล่นประยุกต์ข้อมูล + ไฮบริดแตกบรรทัด (`1+3+1`) เปลี่ยนข้อมูลลึกเป็นลูกเล่นการเล่า (หักมุมตัวเลข / แกะรอยจากของ 1 ชิ้น / ถอดรหัสคำพูด / ตัดสลับ 2 โลก / กฎเหล็กจากแผลในอดีต) สำหรับมุมปกติ\n"
                "    4. กล่องที่ 4 (`style_id: 'applied_hybrid_131'`, `angle_type: 'spin'`, `label: 'แนวที่ 2 (ลูกเล่น 1+3+1) • มุมบิดประเด็น'`): สกิลลูกเล่นประยุกต์ข้อมูล + ไฮบริดแตกบรรทัด (`1+3+1`) สำหรับมุมบิดประเด็น (Room 2)\n"
                "  • **กรณีที่ 2 (ถ้าข่าวต้นทางสั้นมาก / เป็นคลิปชาวบ้านหรือ CCTV เดี่ยว ที่ค้น `search_web` แล้วไม่มีข้อมูลจริงอีกห้องหนึ่งให้บิดประเด็น):** **ห้ามแต่งเรื่องหรือมั่วข้อมูลขึ้นมาเองเด็ดขาด!** ให้ทำเท่าที่มีข้อเท็จจริงจริงรองรับ 100% เท่านั้น\n"
                f"{core_rules}"
            )
    elif output_mode == "strict_clip_only":
        mode_instruction = (
            f"โหมดที่เลือก: แกะเฉพาะในคลิป/โพสต์ 100% ด้วย {s_info['full_badge']} (`strict_clip_only` -> สร้าง 1 กล่องข่าวใน `articles` โดย `label` = `'เฉพาะในคลิป 100%'`)\n"
            "  • ใช้เฉพาะข้อมูลที่ได้จากคลิป/ภาพ/ข้อความต้นทาง 100% เท่านั้น (ห้ามใช้ `search_web` ค้นหาข่าวภายนอกมาปนเด็ดขาด) แล้วสรุปใจความข่าวครบวงจรตามโครงสร้างของชุดสกิลที่เลือก"
        )
    elif output_mode == "viral_3_para":
        mode_instruction = (
            f"โหมดที่เลือก: ข่าวสั้นไวรัล 3 ย่อหน้า (จับคู่คลัง 254 DNA Prompts) ด้วย {s_info['full_badge']} (`viral_3_para` -> สร้าง 2 กล่องข่าวใน `articles` คือ มุมปกติ 3 ย่อหน้า และ มุมบิด 3 ย่อหน้า)\n"
            "  • ค้นเช็กข้อเท็จจริงตามแนวทางของชุดสกิลที่เลือก แล้วเขียนด้วยสกิล `viral-news-writer` คู่กับ `viral-prompt-library` (3 ย่อหน้ากระชับ จับ 1 ประเด็นบริสุทธิ์ เปิดคมไร้คำเกริ่นเรียงความ จบด้วยข้อเท็จจริงคมๆ)"
        )
    elif output_mode == "normal_1_spin_1":
        mode_instruction = (
            f"โหมดที่เลือก: **มุมปกติ 1 ข่าว + บิดประเด็น 1 ข่าว** ด้วย **{s_info['full_badge']}** (`normal_1_spin_1` -> สร้าง **2 กล่องข่าวแยกกัน** ในอาร์เรย์ `articles`):\n"
            f"  1. กล่องที่ 1 (`label`: `'มุมปกติ'`, `title`: `'1. ข่าวแบบมุมปกติ — {s_info['short_tag']}'`, `style_id`: `'{s_info['id']}'`, `angle_type`: `'normal'`): สรุปใจความข่าวหลักครบวงจรตามหลักมุมปกติของ {s_info['short_tag']} (Room 1)\n"
            f"  2. กล่องที่ 2 (`label`: `'บิดประเด็น'`, `title`: `'2. ข่าวแบบบิดประเด็น — {s_info['short_tag']}'`, `style_id`: `'{s_info['id']}'`, `angle_type`: `'spin'`): บิดประเด็นตามหลักมุมบิดของ {s_info['short_tag']} (Room 2)\n"
            f"{core_rules}"
        )
    elif output_mode == "normal_1_spin_2":
        mode_instruction = (
            f"โหมดที่เลือก: **มุมปกติ 1 ข่าว + บิดประเด็น 2 ข่าว** ด้วย **{s_info['full_badge']}** (`normal_1_spin_2` -> สร้าง **3 กล่องข่าวแยกกัน** ในอาร์เรย์ `articles`):\n"
            f"  1. กล่องที่ 1 (`label`: `'มุมปกติ'`, `title`: `'1. ข่าวแบบมุมปกติ — {s_info['short_tag']}'`, `style_id`: `'{s_info['id']}'`, `angle_type`: `'normal'`): สรุปใจความข่าวหลักตามหลักของ {s_info['short_tag']} (Room 1)\n"
            f"  2. กล่องที่ 2 (`label`: `'บิดประเด็น 1'`, `title`: `'2. ข่าวแบบบิดประเด็น มุมที่ 1 — {s_info['short_tag']}'`, `style_id`: `'{s_info['id']}'`, `angle_type`: `'spin'`): บิดประเด็นมุมที่ 1 (Room 2)\n"
            f"  3. กล่องที่ 3 (`label`: `'บิดประเด็น 2'`, `title`: `'3. ข่าวแบบบิดประเด็น มุมที่ 2 — {s_info['short_tag']}'`, `style_id`: `'{s_info['id']}'`, `angle_type`: `'spin'`): บิดประเด็นมุมที่ 2 (Room 3)\n"
            f"{core_rules}"
        )
    elif output_mode == "spin_only_1":
        mode_instruction = (
            f"โหมดที่เลือก: **บิดประเด็นล้วน 1 ข่าว** ด้วย **{s_info['full_badge']}** (`spin_only_1` -> สร้าง **1 กล่องข่าว** ในอาร์เรย์ `articles` โดย `label` = `'บิดประเด็น'`, `style_id` = `'{s_info['id']}'`, `angle_type` = `'spin'`):\n"
            f"  • เจาะมุมบิดตามหลักของ {s_info['short_tag']} และความต้องการของพนักงาน โดยมีสะพานปูที่มาข่าวหลักสั้นๆ ครึ่งถึง 1 บรรทัด (กฎ 95/5)\n"
            f"{core_rules}"
        )
    else:
        mode_instruction = (
            f"โหมดที่เลือก: **มุมปกติ 1 ข่าว** ด้วย **{s_info['full_badge']}** (`standard_4lock` -> สร้าง **1 กล่องข่าว** ในอาร์เรย์ `articles` โดย `label` = `'มุมปกติ'`, `style_id` = `'{s_info['id']}'`, `angle_type` = `'normal'`):\n"
            f"{core_rules}"
        )

    focus_str = (
        f"\n🚨 **คำสั่งบังคับสูงสุดจากช่องความต้องการของพนักงาน (`focus_instruction` — สำคัญอันดับ 1 เหนือค่าเริ่มต้นทุกข้อ):**\n"
        f"👉 **\"{focus_instruction}\"**\n"
        f"  • **บังคับในการค้นข้อมูล (`search_web`):** ต้องตั้งคำค้น `search_web` เพื่อสืบหาข้อเท็จจริง ประวัติ เหตุการณ์ตั้งแต่ต้น หรือประเด็นเฉพาะที่พนักงานระบุในคำสั่งด้านบนโดยตรง!\n"
        f"  • **บังคับในการเลือกมุมและโทนภาษาในทุกกล่อง:** ต้องยึดมุมประเด็น โทนอารมณ์ (เช่น ซอฟๆ / ชื่นชมจากใจจริง / ใส่อินเนอร์ตัวละคร / เจาะเหตุผลกฎหมาย) ตามที่พนักงานสั่งในข้อความด้านบน 100% ห้ามเพิกเฉยหรือเขียนหลุดจากโจทย์ที่พนักงานสั่งเด็ดขาด!\n"
        if focus_instruction
        else ""
    )

    prompt = f"""คุณคือระบบวิเคราะห์ สรุปใจความข่าว และบิดประเด็นข่าวไวรัลความแม่นยำสูง โดยงานนี้เลือกใช้ **{s_info['full_badge']}**
ในโฟลเดอร์งาน `{job_dir.as_posix()}` มีไฟล์ที่เตรียมไว้เรียบร้อยแล้ว ได้แก่: {files_list_str}

ขั้นตอนการทำงานบังคับ:
1. ใช้เครื่องมือ `read_file` เปิดอ่านไฟล์ทั้งหมดในรายการด้านบนเป็นอันดับแรก:
   - **ต้องอ่าน `SKILL_GUIDES.md` เป็นอันดับแรก** เพื่อดูการ์ด 254 DNA Prompts ที่คัดกรองไว้ให้ และยึดกฎการรีเสิร์ช โครงสร้างมุมปกติ โครงสร้างมุมบิด และกฎข้อห้ามของ **{s_info['full_badge']}** แบบ 100%
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
   - Lock 4 (Mandatory Skill-Specific Search Execution): ในทุกโหมด (ยกเว้น `strict_clip_only`) **ต้องเรียกใช้ `search_web` จริงเสมออย่างน้อย 2–4 ครั้ง** ตามขั้นตอนการรีเสิร์ชใน `SKILL_GUIDES.md` ห้ามหยุดค้นแค่ชั้นเดียวแล้วเอาเนื้อหากล่องแรกมาแบ่งครึ่งเขียนซ้ำเด็ดขาด!

เมื่อเขียนและขัดเกลาซ้ำหลายรอบจนได้มาตรฐาน 97–98/100 ทุกบรรทัดแล้ว (เช็กซ้ำว่ายึดกฎ Single-Core Purity ไม่เอาข้อมูลหลุดโทนมาปน, ไม่จำกัดจำนวนคำตายตัวแต่ให้สั้นหรือยาวตามกลไกและข้อเท็จจริงจริงของข่าวประเด็นนั้นๆ, ไร้คำต้องห้าม 'เบื้องหลัง...' / 'จึงสะท้อนชัดเจนว่า...', ไร้คำอังกฤษปนไทยในเนื้อข่าว, และใน `content` ของแต่ละกล่องใส่เฉพาะตัวเนื้อข่าวล้วนๆ ห้ามใส่หัวข้อนำหน้าซ้ำใน `content` และห้ามครอบด้วย ```text) ให้บันทึกผลลัพธ์ลงไฟล์ `{job_dir.as_posix()}/result.json` ด้วย `write_file` ทันที (เมื่อ `write_file` บันทึก `result.json` สำเร็จแล้ว ให้ตอบสั้นๆ เพียงคำว่า `"SAVED_OK"` เท่านั้น ห้ามพิมพ์ก้อน JSON ซ้ำออกมาทางหน้าจอเด็ดขาด!) ตามโครงสร้างนี้เท่านั้น:
{{
  "headline": "หัวข้อสรุปเคสข่าวสั้นๆ เข้าใจทันที",
  "skill_set": "{s_info['id']}",
  "skill_set_label": "{s_info['full_badge']}",
  "articles": [
    {{
      "style_id": "toon_dna_121",
      "angle_type": "normal",
      "label": "แนวที่ 1 (3 ย่อหน้า • 254 DNA) • มุมปกติ",
      "title": "แนวที่ 1: 3 ย่อหน้ากระชับเปี่ยมคุณค่า (1+1+1 • โมเดล จิ๊ก เนาวรัตน์) — มุมปกติ",
      "style_meta": "การ์ด #xxx [prompt_xxxx] • 98/100",
      "source_url": "https://... (ลิงก์ต้นทางฉบับจริง Direct Source URL)",
      "content": "ย่อหน้าที่ 1 (ผสานท่าเปิดจากการ์ด 254 แนว ฮุคเข้าใจจบทั้งข่าวในประโยคเดียว)\\n\\nย่อหน้าที่ 2 (เจาะลึกจุดเริ่มต้น + ดีเทลวิธีปฏิบัติที่เห็นหัวใจแบบกระชับ 1–2 ประโยค ยึด Single-Core Purity)\\n\\nย่อหน้าที่ 3 (ขมวดจบด้วยคุณค่าทางใจหรือมุมคิดจริงของตัวละคร)"
    }},
    {{
      "style_id": "toon_dna_121",
      "angle_type": "spin",
      "label": "แนวที่ 1 (3 ย่อหน้า • 254 DNA) • มุมบิดประเด็น",
      "title": "แนวที่ 1: 3 ย่อหน้ากระชับเปี่ยมคุณค่า (1+1+1 • โมเดล จิ๊ก เนาวรัตน์) — มุมบิดประเด็น",
      "style_meta": "การ์ด #xxx [prompt_xxxx] • 98/100",
      "source_url": "https://... (ลิงก์ต้นทางฉบับจริง Direct Source URL)",
      "content": "ย่อหน้าที่ 1 (ฮุคมุมบิดประเด็นคนละห้องข้อมูลกับมุมปกติ เข้าใจจบทั้งเรื่องในประโยคเดียว)\\n\\nย่อหน้าที่ 2 (เจาะลึกที่มา/เหตุการณ์จริงชั้นที่ 2 + ดีเทลวิธีปฏิบัติที่เห็นหัวใจแบบกระชับ 1–2 ประโยค)\\n\\nย่อหน้าที่ 3 (ขมวดจบด้วยคุณค่าทางใจหรือมุมคิดจริงของตัวละคร)"
    }},
    {{
      "style_id": "applied_hybrid_131",
      "angle_type": "normal",
      "label": "แนวที่ 2 (ลูกเล่น 1+3+1) • มุมปกติ",
      "title": "แนวที่ 2: สกิลลูกเล่นประยุกต์ข้อมูล + ไฮบริดแตกบรรทัด (1+3+1 • 98/100) — มุมปกติ",
      "style_meta": "ลูกเล่นประยุกต์ข้อมูล • 98/100",
      "source_url": "https://... (ลิงก์ต้นทางฉบับจริง Direct Source URL)",
      "content": "บรรทัดเปิดเรื่องด้วยลูกเล่นคมๆ 1 บรรทัด\\n\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 1\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 2\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 3\\n\\nบรรทัดขมวดจบตามธรรมชาติ 1 บรรทัด"
    }},
    {{
      "style_id": "applied_hybrid_131",
      "angle_type": "spin",
      "label": "แนวที่ 2 (ลูกเล่น 1+3+1) • มุมบิดประเด็น",
      "title": "แนวที่ 2: สกิลลูกเล่นประยุกต์ข้อมูล + ไฮบริดแตกบรรทัด (1+3+1 • 98/100) — มุมบิดประเด็น",
      "style_meta": "ลูกเล่นประยุกต์ข้อมูล • 98/100",
      "source_url": "https://... (ลิงก์ต้นทางฉบับจริง Direct Source URL)",
      "content": "บรรทัดเปิดเรื่องมุมบิดด้วยลูกเล่น (คนละห้องข้อมูลกับมุมปกติ) 1 บรรทัด\\n\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 1\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 2\\nบรรทัดเนื้อหาเจาะลึกจุดที่ 3\\n\\nบรรทัดขมวดจบตามธรรมชาติ 1 บรรทัด"
    }}
  ],
  "raw_news": "เนื้อข่าวรวมทุกกล่อง",
  "visual_audio_breakdown": "สรุปข้อเท็จจริงดิบที่สกัดได้จากภาพ-เสียงในคลิปหรือบทความต้นทางแบบละเอียด",
  "context_connected": "ระบุผลลัพธ์การค้นหาจริง แยกเป็น (ชั้นที่ 1: ข้อมูลต้นทางฉบับเต็ม), (ชั้นที่ 2: ข้อมูลเจาะลึกที่ค้นเจอจริงจาก search_web), และ (การ์ด Prompt ที่จับคู่จาก 254 แนว + คะแนนคุณภาพ 98/100)",
  "audit_tag": "✅ {s_info['full_badge']} • [Prompt DNA Matched • มาตรฐาน 98/100] • [ที่มาข้อมูล: ระบุแหล่งต้นทางจริง + ข้อมูลชั้นที่ 2 ที่ค้นตรวจซ้ำ]",
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

    result_json_path = job_dir / "result.json"
    agy_res = await run_agy_with_result_watcher(cmd_agy, cwd=job_dir, result_json_path=result_json_path, timeout=480)
    progress_cb("กำลังจัดรูปแบบผลลัพธ์และบันทึกข้อมูล...", 92)

    result_data: Optional[Dict[str, Any]] = None

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
