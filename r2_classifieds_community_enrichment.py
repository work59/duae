"""
R2 Classifieds + Community phone + description enrichment.

Flow:
1) Prepare: read yesterday's DUAE/classifieds and DUAE/community Excel files from R2,
   and create scrape jobs containing at most 15 listings each.
2) Scrape: each job uses Camoufox to fetch phone and, only when description_full is empty,
   the full description.
3) Combine: merge all job results back into the original Excel sheets and update JSON files.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

R2_ACCESS_KEY = os.getenv("CF_R2_ACCESS_KEY_ID")
R2_SECRET_KEY = os.getenv("CF_R2_SECRET_ACCESS_KEY")
R2_ENDPOINT = os.getenv("CF_R2_ENDPOINT_URL", "").rstrip("/")
R2_BUCKET = os.getenv("CF_R2_BUCKET_NAME", "")
MOTORS_PREFIX = os.getenv("MOTORS_PREFIX", "DUAE")
DUBAI_TZ = "Asia/Dubai"

PHONE_COLUMN = "contact_phone_number"
DESCRIPTION_COLUMN = "description_full"
USER_COLUMN = "user"
USER_ID_COLUMN = "user_id"
LEGACY_ID_COLUMN = "legacy_id"


def r2_client():
    if not all([R2_ACCESS_KEY, R2_SECRET_KEY, R2_ENDPOINT, R2_BUCKET]):
        raise RuntimeError("Missing R2 environment variables.")
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def yesterday_prefix(date_str: str | None = None) -> tuple[str, str]:
    if date_str:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    else:
        now = datetime.now(timezone.utc)
        dubai_now = now.astimezone(__import__("zoneinfo").ZoneInfo(DUBAI_TZ))
        target = dubai_now.date() - timedelta(days=1)
    prefix = f"{MOTORS_PREFIX}/year={target.year}/month={target.month:02d}/day={target.day:02d}/"
    return target.isoformat(), prefix


def list_keys(client, prefix: str) -> list[str]:
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".xlsx") and "/users-data/" not in key:
                keys.append(key)
    return sorted(keys)


def download_bytes(client, key: str) -> bytes:
    return client.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read()


def upload_bytes(client, key: str, data: bytes, content_type: str):
    client.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
    )


def parse_user(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    s = value.strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    try:
        obj = ast.literal_eval(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def user_id_from_value(value: Any) -> str | None:
    user = parse_user(value)
    value = user.get("id")
    if value is None or str(value).strip() in {"", "nan", "None"}:
        return None
    return str(value).strip()


def legacy_id_from_value(value: Any) -> str | None:
    user = parse_user(value)
    value = user.get("legacy_id")
    if value is None or str(value).strip() in {"", "nan", "None"}:
        return None
    return str(value).strip()


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return str(value).strip().lower() in {"", "nan", "none", "null"}


def excel_sheets(data: bytes) -> dict[str, pd.DataFrame]:
    return pd.read_excel(io.BytesIO(data), sheet_name=None)


def clean_excel_value(value):
    if isinstance(value, str):
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", value)
    return value


def prepare(date_str: str | None, out_dir: str):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jobs_dir = out / "jobs"
    jobs_dir.mkdir(exist_ok=True)

    date_iso, prefix = yesterday_prefix(date_str)
    client = r2_client()

    classifieds_prefix = f"{prefix}classified/"
    community_prefix = f"{prefix}community/"

    keys = []
    keys.extend(list_keys(client, classifieds_prefix))
    keys.extend(list_keys(client, community_prefix))

    print(f"[PREPARE] Date: {date_iso}")
    print(f"[PREPARE] Found {len(keys)} Excel file(s).")

    work = []

    for key in keys:
        sheets = excel_sheets(download_bytes(client, key))
        for sheet_name, df in sheets.items():
            if "id" not in df.columns:
                continue

            for row_pos, (_, row) in enumerate(df.iterrows()):
                listing_id = row.get("id")
                if is_empty(listing_id):
                    continue
                listing_id = str(listing_id)

                uid = user_id_from_value(row.get(USER_COLUMN))

                phone_missing = is_empty(row.get(PHONE_COLUMN))
                description_missing = is_empty(row.get(DESCRIPTION_COLUMN))

                need_phone = phone_missing
                need_description = description_missing

                if not need_phone and not need_description:
                    continue

                work.append({
                    "file_key": key,
                    "sheet_name": sheet_name,
                    "row_position": row_pos,
                    "id": listing_id,
                    "absolute_url": row.get("absolute_url"),
                    USER_COLUMN: row.get(USER_COLUMN),
                    USER_ID_COLUMN: uid,
                    "need_phone": bool(need_phone),
                    "need_description": bool(need_description),
                })

    chunks = [work[i:i + 15] for i in range(0, len(work), 15)]
    manifest = []

    for idx, chunk in enumerate(chunks):
        path = jobs_dir / f"job_{idx:05d}.json"
        path.write_text(json.dumps(chunk, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        manifest.append(str(path.relative_to(out)))

    (out / "manifest.json").write_text(
        json.dumps({
            "date": date_iso,
            "prefix": prefix,
            "jobs": manifest,
            "total_work_items": len(work),
            "total_jobs": len(chunks),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[PREPARE] Work items: {len(work)}")
    print(f"[PREPARE] Jobs: {len(chunks)} (max 15 items/job)")
    print(f"[PREPARE] Manifest: {out / 'manifest.json'}")


def extract_en_url(raw: Any) -> str | None:
    if isinstance(raw, dict):
        return raw.get("en") or raw.get("ar")
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith("{"):
        try:
            obj = ast.literal_eval(s)
            if isinstance(obj, dict):
                return obj.get("en") or obj.get("ar")
        except Exception:
            pass
    return s


BUTTON_SELECTORS = [
    'button:has-text("Show Phone Number")',
    'button:has-text("Show Number")',
    'button:has-text("Show phone number")',
    'button:has-text("Call")',
    '[data-testid="profile-call-button"]',
    '[data-testid="call-cta-button"]',
    '[data-testid*="phone" i]',
    '[data-testid*="call" i]',
]

CHALLENGE_MARKERS = [
    "Pardon Our Interruption",
    "Additional security check is required",
    "I am human",
    "hCaptcha",
    "reeseSkipExpirationCheck",
]


def is_challenge_page(html: str) -> bool:
    return any(x in html for x in CHALLENGE_MARKERS)


def find_phone_recursive(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if "phone" in str(key).lower() and isinstance(value, (str, int)) and value:
                return str(value)
        for value in obj.values():
            found = find_phone_recursive(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_phone_recursive(item)
            if found:
                return found
    return None


def extract_description(page) -> str | None:
    try:
        see_more = page.locator('button:has-text("See full description")').first
        if see_more.is_visible(timeout=2000):
            see_more.click()
            page.wait_for_timeout(1500)
    except Exception:
        pass

    selectors = [
        'div[data-testid="description"]',
        '[data-testid="description"] + div',
        '[data-testid="description"] ~ div',
        '[data-testid="description-heading"]',
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=3000):
                text = loc.inner_text()
                if text and text.strip() and text.strip().lower() != "description":
                    return clean_excel_value(text.strip())
        except Exception:
            continue

    try:
        heading = page.locator('[data-testid="description"]').first
        if heading.is_visible(timeout=2000):
            text = heading.inner_text()
            if text and text.strip().lower() == "description":
                parent = page.locator('xpath=//*[@data-testid="description"]/..')
                divs = parent.locator('div').all()
                best = None
                for div in divs:
                    try:
                        t = div.inner_text()
                        if t and len(t.strip()) > len(best or ""):
                            best = t
                    except Exception:
                        pass
                if best and best.strip().lower() != "description":
                    return clean_excel_value(best.strip())
            elif text and text.strip():
                return clean_excel_value(text.strip())
    except Exception:
        pass

    return None


def reveal_phone(page, timeout_ms=10000):
    captured = {"data": None}

    def handle_response(response):
        if "listing-profile" not in response.url or response.status != 200:
            return
        try:
            captured["data"] = response.json()
        except Exception:
            pass

    page.on("response", handle_response)
    button = None

    for selector in BUTTON_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=3500):
                button = loc
                break
        except Exception:
            continue

    if button is None:
        page.remove_listener("response", handle_response)
        return None, "button_not_found"

    try:
        button.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        try:
            button.click(timeout=6000)
        except Exception:
            button.click(force=True)

        waited = 0
        while captured["data"] is None and waited < timeout_ms:
            page.wait_for_timeout(400)
            waited += 400
    except Exception as exc:
        page.remove_listener("response", handle_response)
        return None, f"click_error: {exc}"
    finally:
        page.remove_listener("response", handle_response)

    if captured["data"] is None:
        return None, "no_response_captured"

    phone = find_phone_recursive(captured["data"])
    if phone is None:
        return None, "phone_field_not_found"
    return phone, "ok"


def scrape_job(job_file: str, output_file: str):
    from camoufox.sync_api import Camoufox
    items = json.loads(Path(job_file).read_text(encoding="utf-8"))
    results = []

    with Camoufox(
        headless=True,
        humanize=True,
        geoip=True,
        block_images=False,
    ) as browser:
        page = browser.new_page()

        for n, item in enumerate(items, 1):
            listing_id = item["id"]

            print(f"\n[{n}/{len(items)}] START listing: {listing_id}")
            print(f"[{n}/{len(items)}] Need phone: {item.get('need_phone')}")
            print(f"[{n}/{len(items)}] Need description: {item.get('need_description')}")

            result = {
                "file_key": item["file_key"],
                "sheet_name": item["sheet_name"],
                "row_position": item["row_position"],
                "id": listing_id,
                USER_COLUMN: item.get(USER_COLUMN),
                USER_ID_COLUMN: item.get(USER_ID_COLUMN),
                "phone": None,
                "description_full": None,
                "phone_status": "not_needed" if not item.get("need_phone") else None,
                "description_status": "not_needed" if not item.get("need_description") else None,
            }

            url = extract_en_url(item.get("absolute_url"))
            if not url:
                print("[ERROR] No URL")
                if item.get("need_phone"):
                    result["phone_status"] = "no_url"
                if item.get("need_description"):
                    result["description_status"] = "no_url"
                results.append(result)
                continue

            print(f"[URL] {url}")

            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )

                page.wait_for_timeout(random.uniform(6000, 10000))

                html = page.content()

                if is_challenge_page(html):
                    print("[CHALLENGE] Imperva challenge detected")

                    result["phone_status"] = (
                        "imperva_challenge"
                        if item.get("need_phone")
                        else result["phone_status"]
                    )

                    result["description_status"] = (
                        "imperva_challenge"
                        if item.get("need_description")
                        else result["description_status"]
                    )

                    result["error"] = "challenge_page"
                    results.append(result)
                    break

                if item.get("need_phone"):
                    phone, status = reveal_phone(page)

                    result["phone"] = phone
                    result["phone_status"] = status

                    if status == "ok":
                        print("[PHONE] SUCCESS")
                    else:
                        print(f"[PHONE] FAILED: {status}")

                if item.get("need_description"):
                    print("[DESCRIPTION] Extracting description...")

                    desc = extract_description(page)

                    result["description_full"] = desc

                    if desc:
                        result["description_status"] = "ok"
                        print("[DESCRIPTION] SUCCESS")
                    else:
                        result["description_status"] = "not_found"
                        print("[DESCRIPTION] FAILED: not_found")

            except Exception as exc:
                print(f"[ERROR] {type(exc).__name__}: {exc}")

                result["error"] = str(exc)

                if item.get("need_phone") and not result["phone_status"]:
                    result["phone_status"] = f"error: {exc}"

                if item.get("need_description") and not result["description_status"]:
                    result["description_status"] = f"error: {exc}"

            results.append(result)

            if n < len(items):
                time.sleep(random.uniform(10, 20))

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)

    Path(output_file).write_text(
        json.dumps(
            results,
            ensure_ascii=False,
            default=str,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\n[JOB DONE] {len(results)}/{len(items)} listings processed")


def build_excel_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=str(sheet_name)[:31], index=False)
    return buf.getvalue()


def build_json_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    records = []
    for sheet_name, df in sheets.items():
        for row in df.to_dict(orient="records"):
            row["_sheet"] = sheet_name
            records.append(row)
    return json.dumps(
        records, ensure_ascii=False, indent=2, default=str
    ).encode("utf-8")


def combine(results_dir: str, date_str: str | None):
    root = Path(results_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    date_iso = manifest["date"]
    prefix = manifest["prefix"]
    client = r2_client()

    result_files = sorted((root / "results").glob("job_*.json"))
    all_results = []
    for path in result_files:
        all_results.extend(json.loads(path.read_text(encoding="utf-8")))

    updates = {}

    for r in all_results:
        key = (r["file_key"], r["sheet_name"], str(r["id"]))
        updates[key] = r

    changed_files = 0

    for key in sorted({r["file_key"] for r in all_results}):
        sheets = excel_sheets(download_bytes(client, key))
        changed = False

        for sheet_name, df in sheets.items():
            if "id" not in df.columns:
                continue
            if PHONE_COLUMN not in df.columns:
                df[PHONE_COLUMN] = None
            if DESCRIPTION_COLUMN not in df.columns:
                df[DESCRIPTION_COLUMN] = None

            for pos, (_, row) in enumerate(df.iterrows()):
                listing_id = row.get("id")
                if is_empty(listing_id):
                    continue
                u = updates.get((key, sheet_name, str(listing_id)))
                if not u:
                    continue

                if not is_empty(u.get("phone")):
                    df.at[df.index[pos], PHONE_COLUMN] = u["phone"]
                    changed = True

                if u.get("description_status") == "ok" and not is_empty(u.get("description_full")):
                    df.at[df.index[pos], DESCRIPTION_COLUMN] = u["description_full"]
                    changed = True

            sheets[sheet_name] = df

        if changed:
            upload_bytes(
                client,
                key,
                build_excel_bytes(sheets),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            json_key = key[:-5] + ".json"
            upload_bytes(client, json_key, build_json_bytes(sheets), "application/json")
            changed_files += 1
            print(f"[COMBINE] Uploaded: {key}")

    summary = {
        "date": date_iso,
        "result_files": len(result_files),
        "result_rows": len(all_results),
        "changed_listing_files": changed_files,
    }
    (root / "combine_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to yesterday in Asia/Dubai")
    p.add_argument("--out", default="work")

    s = sub.add_parser("scrape")
    s.add_argument("--job", required=True)
    s.add_argument("--output", required=True)

    c = sub.add_parser("combine")
    c.add_argument("--date", default=None)
    c.add_argument("--work", default="work")

    args = parser.parse_args()

    if args.command == "prepare":
        prepare(args.date, args.out)
    elif args.command == "scrape":
        scrape_job(args.job, args.output)
    elif args.command == "combine":
        combine(args.work, args.date)


if __name__ == "__main__":
    main()