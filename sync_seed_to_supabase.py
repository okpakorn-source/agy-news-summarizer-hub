import os
import json
import sqlite3
from pathlib import Path
from supabase_client import (
    upload_file_to_supabase,
    save_job_to_supabase,
    update_host_heartbeat,
    list_jobs_from_supabase,
)

BASE_DIR = Path(__file__).parent.resolve()
DB_PATH = BASE_DIR / "jobs.db"
JOBS_DIR = BASE_DIR / "jobs_data"


def sync_all_local_jobs_to_supabase():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at ASC").fetchall()

    for r in rows:
        item = dict(r)
        job_id = item["id"]
        urls = json.loads(item["urls_json"] or "[]")
        result = json.loads(item["result_json"]) if item["result_json"] else None

        job_dir = JOBS_DIR / job_id
        if result and job_dir.exists():
            # Upload contact sheets to Supabase Storage
            new_sheets = []
            for s_url in result.get("contact_sheets", []):
                fname = s_url.split("/")[-1]
                local_f = job_dir / fname
                if local_f.exists():
                    ctype = "image/png" if fname.endswith(".png") else "image/jpeg"
                    pub_url = upload_file_to_supabase(f"{job_id}/{fname}", local_f.read_bytes(), ctype)
                    new_sheets.append(pub_url)
                    print(f"Uploaded sheet: {pub_url}")
                else:
                    new_sheets.append(s_url)
            result["contact_sheets"] = new_sheets

            # Upload uploaded_images to Supabase Storage
            new_imgs = []
            for img_url in result.get("uploaded_images", []):
                fname = img_url.split("/")[-1]
                local_f = job_dir / fname
                if local_f.exists():
                    ctype = "image/png" if fname.endswith(".png") else "image/jpeg"
                    pub_url = upload_file_to_supabase(f"{job_id}/{fname}", local_f.read_bytes(), ctype)
                    new_imgs.append(pub_url)
                    print(f"Uploaded image: {pub_url}")
                else:
                    new_imgs.append(img_url)
            result["uploaded_images"] = new_imgs

            # Update local SQLite with cloud URLs as well
            conn.execute(
                "UPDATE jobs SET result_json = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False), job_id),
            )
            conn.commit()

        job_doc = {
            "id": job_id,
            "employee_name": item["employee_name"],
            "urls": urls,
            "raw_text": item["raw_text"],
            "focus_instruction": item["focus_instruction"],
            "output_mode": item["output_mode"],
            "status": item["status"],
            "progress_text": item["progress_text"],
            "progress_pct": item["progress_pct"],
            "result": result,
            "created_at": item["created_at"],
            "completed_at": item["completed_at"],
        }
        ok = save_job_to_supabase(job_doc)
        print(f"Synced job {job_id} to Supabase: {ok}")

    conn.close()
    update_host_heartbeat("http://192.168.1.148:8080", "https://wallpapers-result-roommates-pike.trycloudflare.com")
    jobs_in_cloud = list_jobs_from_supabase()
    print(f"Total jobs in Supabase: {len(jobs_in_cloud)}")


if __name__ == "__main__":
    sync_all_local_jobs_to_supabase()
