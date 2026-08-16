import pandas as pd
import json
import ast
import os
import re
import io
import random
import time
import requests as req
from PIL import Image
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from r2_uploader import upload_buffer

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
# Long-edge cap (px) images are downscaled to before upload, plus the WEBP
# quality used when re-encoding. Most source photos are 3000px+ wide;
# capping the long edge is what actually cuts stored bytes -- quality alone
# only goes so far.
MAX_IMAGE_DIMENSION = 1280
WEBP_QUALITY = 65

COLUMNS_TO_DROP = [
    "photo", "photo_mains", "photos", "_highlightResult",
    "site_categories_slug_tree", "category_slug_tree", "category_tree",
    "category", "permalink", "seo_links", "vas"
]


def get_category_path(category_v2_value) -> str:
    """Build R2 folder path from category_v2.slug_paths dynamically.

    Example:
        slug_paths: ['motors', 'motors/number-plates', 'motors/number-plates/dubai-plate', 'motors/number-plates/dubai-plate/private-car']
        -> deepest: 'motors/number-plates/dubai-plate/private-car'  (slug_paths is shallow -> deep, so the LAST element is deepest)
        -> parent:  'motors/number-plates'  (keep first 2 segments after 'motors')
        -> result:  'motors/number-plates'

    For motors, we take the first 2 meaningful segments (motors + sub-category).
    """
    cat = parse_dict_field(category_v2_value)
    slug_paths = cat.get("slug_paths", [])
    if not slug_paths:
        return "unknown"

    # slug_paths is ordered shallow -> deep, so the deepest path is the LAST element
    deepest = slug_paths[-1]
    parts = deepest.split("/")

    # For motors: keep first 2 segments (e.g. motors/number-plates)
    # If only 1 segment, use it as-is
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return deepest


def parse_dict_field(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            try:
                return ast.literal_eval(value)
            except Exception:
                return {}
    return {}


def get_city_name(site_value) -> str:
    site = parse_dict_field(site_value)
    if not site:
        return "Unknown"

    if "en" in site:
        city = site.get("en", "Unknown")
    else:
        name_field = site.get("name")
        if isinstance(name_field, dict):
            city = name_field.get("en", "Unknown")
        elif isinstance(name_field, str):
            city = name_field
        else:
            city = "Unknown"

    CITY_MAPPING = {
        "Ras al Khaimah": "Ras Al Khaimah",
        "Umm al Quwain": "Umm Al Quwain",
    }

    return CITY_MAPPING.get(city, city)


def get_category_names(category_v2_value) -> list:
    cat = parse_dict_field(category_v2_value)
    return cat.get("names_en", [])


def sanitize_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", str(name))
    name = name.replace(" ", "_")
    name = re.sub(r'_+', '_', name)
    return name.strip("_")


def extract_sheet_name(names_en: list) -> str:
    if not names_en:
        return "Other"
    if len(names_en) <= 2:
        return names_en[-1]
    return names_en[2]


def generate_data_quality_report(df: pd.DataFrame, total_rows: int) -> str:
    report_lines = ["--- Data Quality Report ---"]
    for col in df.columns:
        missing = df[col].isna().sum() + (df[col] == '').sum()
        pct = (missing / total_rows) * 100 if total_rows > 0 else 0
        report_lines.append(f'  {col}: {missing} empty ({pct:.2f}%)')
    return "\n".join(report_lines)


def load_all_hits(jsonl_files: list) -> pd.DataFrame:
    rows = []
    for path in jsonl_files:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    df = pd.DataFrame(rows)

    existing_cols = [c for c in COLUMNS_TO_DROP if c in df.columns]
    if existing_cols:
        df = df.drop(columns=existing_cols)
        print(f"  Dropped columns: {existing_cols}")

    return df


def _get_english_url(absolute_url_value):
    parsed = parse_dict_field(absolute_url_value)
    if isinstance(parsed, dict):
        return parsed.get("en") or parsed.get("ar")
    if isinstance(absolute_url_value, str):
        return absolute_url_value
    return None


DESCRIPTION_SELECTORS = [
    '[data-testid="description"]',
    '[data-testid="description-heading"]',
]

def _extract_description(page):
    for selector in DESCRIPTION_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=3000):
                text = loc.inner_text()
                if text:
                    return text
        except Exception:
            continue
    return None

def clean_for_excel(val):
    """Remove control characters illegal in Excel cells."""
    if isinstance(val, str):
        # Remove control chars except newline, carriage return, and tab
        return re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', val)
    return val

def enrich_with_description(
    df: pd.DataFrame,
    url_column: str = "absolute_url",
    headless: bool = True,
    min_delay: float = 5,
    max_delay: float = 12,
) -> pd.DataFrame:
    df = df.copy()
    description_col = [None] * len(df)

    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=headless, channel="chrome")
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="Asia/Dubai",
        )
        page = context.new_page()

        for pos, (idx, row) in enumerate(df.iterrows()):
            url = _get_english_url(row.get(url_column))
            if not url:
                print(f"  [{pos + 1}/{len(df)}] Skipped - no URL")
                continue

            print(f"  [{pos + 1}/{len(df)}] Visiting: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(random.uniform(1500, 3000))

                html = page.content()
                if "Pardon Our Interruption" in html:
                    print("    -> Imperva challenge hit, stopping enrichment.")
                    break

                description_col[pos] = clean_for_excel(_extract_description(page))

            except Exception as e:
                print(f"    -> FAILED: {e}")

            if pos < len(df) - 1:
                delay = random.uniform(min_delay, max_delay)
                time.sleep(delay)

        page.close()
        browser.close()
    df["description_full"] = description_col
    return df


def download_images(images: list, slug: str = "", category: str = "", id_prod: str = "",
                     cat0: str = "", cat1: str = "") -> list:
    r2_paths = []
    uploaded = 0
    failed = 0

    if not images or not isinstance(images, list):
        return r2_paths

    ext = "webp"
    slug = slug or "unknown"
    file_prefix = id_prod if id_prod else slug
    today = datetime.now(timezone.utc)

    category_display = f"{cat0}/{cat1}" if cat0 and cat1 else (cat1 or cat0)

    for idx, img_url in enumerate(images, start=1):
        filename = f"{file_prefix}-{idx}.{ext}"
        try:
            r = req.get(img_url, timeout=15)
            if r.status_code == 200:
                img = Image.open(io.BytesIO(r.content))
                img = img.convert("RGB")
                output_buffer = io.BytesIO()
                img.save(output_buffer, format="WEBP", quality=WEBP_QUALITY, method=6)
                output_buffer.seek(0)

                r2_key = upload_buffer(
                    output_buffer,
                    filename=filename,
                    folder_name="DUAE",
                    category=category,
                    file_type="images",
                    content_type="image/webp",
                    dt=today,
                    category_display=category_display
                )
                if r2_key:
                    r2_paths.append(r2_key)
                    uploaded += 1
                else:
                    failed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"    [ERROR] {filename} image {idx}: {e}")
            failed += 1

    if uploaded or failed:
        print(f"    {file_prefix}: {uploaded} uploaded, {failed} failed out of {len(images)}")
    return r2_paths


def process_images_for_group(df: pd.DataFrame, category: str, cat0: str, cat1: str,
                              workers: int = 2) -> pd.DataFrame:
    df = df.copy()
    n = len(df)
    results = [None] * n

    def worker(pos: int, images: list, slug: str, id_prod: str) -> tuple:
        r2_paths = download_images(
            images, slug=slug, category=category, id_prod=id_prod,
            cat0=cat0, cat1=cat1
        )
        return pos, r2_paths

    tasks = []
    for pos, (idx, row) in enumerate(df.iterrows()):
        images = row.get("photo_thumbnails", [])
        id_prod = str(row.get("id", idx))
        slug = id_prod
        tasks.append((pos, images, slug, id_prod))

    print(f"  Downloading images for {n} products using {workers} workers...")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(worker, pos, images, slug, id_prod): pos for pos, images, slug, id_prod in tasks}

        completed = 0
        for future in as_completed(futures):
            try:
                pos, r2_paths = future.result(timeout=120)
                results[pos] = r2_paths
            except Exception as e:
                pos = futures[future]
                print(f"    [ERROR] Task {pos} failed: {e}")
                results[pos] = []

            completed += 1
            if completed % 50 == 0 or completed == n:
                print(f"    Progress: {completed}/{n}")

    df["images_r2_paths"] = results
    return df


def _write_excel_and_json(sheets: dict, xlsx_path: str) -> tuple:
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    json_path = xlsx_path.replace(".xlsx", ".json")
    all_records = []
    for sheet_name, df in sheets.items():
        records = df.to_dict(orient="records")
        for r in records:
            r["_sheet"] = sheet_name
        all_records.extend(records)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2, default=str)

    return xlsx_path, json_path


# =============================================================================
# Summary helpers (DKSA-style)
# =============================================================================

def format_failed_summary(failed_items: list, max_len: int = 400) -> str | None:
    """Format failed items into a short summary string."""
    if not failed_items:
        return None
    parts = []
    for item in failed_items[:12]:
        name = item.get("name", "?")
        count = item.get("errors", 0)
        detail = item.get("detail", "")
        bit = f"{name}: {count} error(s)"
        if detail:
            bit += f" ({detail})"
        parts.append(bit)
    text = "; ".join(parts)
    if len(failed_items) > 12:
        text += f"; +{len(failed_items) - 12} more"
    return text[:max_len]


def read_request_stats(output_base_dir: str) -> dict:
    """Read request_stats_*.json files if they exist."""
    stats_files = []
    for f in os.listdir(output_base_dir) if os.path.exists(output_base_dir) else []:
        if f.startswith("request_stats_") and f.endswith(".json"):
            stats_files.append(os.path.join(output_base_dir, f))

    total_requests = 0
    total_duration_min = 0.0
    for sf in stats_files:
        try:
            with open(sf, "r", encoding="utf-8") as fh:
                s = json.load(fh)
            total_requests += s.get("total_requests", 0)
            total_duration_min += s.get("total_duration_min", 0) or 0
        except Exception:
            pass

    requests_per_min = round(total_requests / total_duration_min, 2) if total_duration_min > 0 else total_requests
    return {
        "requests_total": total_requests,
        "duration_sec": round(total_duration_min * 60, 2),
        "requests_per_min": requests_per_min,
    }


def read_failed_pages(output_base_dir: str) -> list:
    """Read failed_pages_*.json files if they exist."""
    failed_files = []
    for f in os.listdir(output_base_dir) if os.path.exists(output_base_dir) else []:
        if f.startswith("failed_pages_") and f.endswith(".json"):
            failed_files.append(os.path.join(output_base_dir, f))

    all_failed = []
    for ff in failed_files:
        try:
            with open(ff, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            all_failed.extend(data.get("failed_pages", []))
        except Exception:
            pass
    return all_failed


def build_group_summary(
    sheets: dict,
    group_df: pd.DataFrame,
    cat0: str,
    cat1: str,
    dt: datetime,
    output_base_dir: str,
    category_path: str = None,
) -> dict:
    """
    DKSA-style summary with request_metrics + failed_items.
    """
    subcategories = [
        {
            "name_ar": "",
            "name_en": name,
            "slug": name,
            "listings_count": len(sdf),
            "has_subcategories": False,
            "subcategories": [],
        }
        for name, sdf in sheets.items()
    ]

    # Read request stats
    request_metrics = read_request_stats(output_base_dir)

    # Read failed pages
    failed_pages = read_failed_pages(output_base_dir)
    total_failed = len(failed_pages)
    request_metrics["requests_failed"] = total_failed

    failed_items = []
    for page in failed_pages:
        failed_items.append({
            "name": f"{page.get('category', 'unknown')}-page-{page.get('page', '?')}",
            "errors": 1,
            "detail": page.get("error", "Failed after retries")
        })

    # Calculate error_rate_pct
    total_requests = request_metrics.get("requests_total", 0)
    if total_requests > 0:
        request_metrics["error_rate_pct"] = round(total_failed / total_requests * 100, 2)
    else:
        request_metrics["error_rate_pct"] = None

    # Calculate requests_per_min from actual duration
    duration_sec = request_metrics.get("duration_sec", 0)
    if duration_sec and duration_sec > 0:
        request_metrics["requests_per_min"] = round(
            total_requests / (duration_sec / 60.0), 2
        )

    return {
        "scraped_at": dt.isoformat(),
        "data_scraped_date": (dt - timedelta(days=1)).strftime("%Y-%m-%d"),
        "saved_to_R2_date": dt.strftime("%Y-%m-%d"),
        "category_path": category_path,
        "category": {
            "name_ar": f"{cat0}/{cat1}",
            "name_en": f"{cat0}/{cat1}",
            "slug": f"{sanitize_name(cat0)}-{sanitize_name(cat1)}",
        },
        "workflow_name": "Classifieds & Community",
        "total_subcategories": len(subcategories),
        "total_listings": len(group_df),
        "subcategories": subcategories,
        "request_metrics": request_metrics,
        "failed_items": failed_items,
        "failed_items_summary": format_failed_summary(failed_items),
    }


def _write_shallow_split(group_df: pd.DataFrame, excel_dir: str, safe_cat1: str) -> tuple:
    """
    max category depth <= 3: single {cat1}.xlsx, one sheet per leaf
    subcategory (names_en[2], or names_en[-1] when there's no
    subcategory level at all).
    """
    group_df = group_df.copy()
    group_df["_sheet_name"] = group_df["_names_en"].apply(extract_sheet_name)

    main_xlsx = os.path.join(excel_dir, f"{safe_cat1}.xlsx")
    sheets = {}
    for sheet_name, sdf in group_df.groupby("_sheet_name"):
        cols_to_drop = ["_sheet_name", "_cat0", "_cat1", "_names_en"]
        sdf_clean = sdf.drop(columns=[c for c in cols_to_drop if c in sdf.columns])
        safe_sheet = sanitize_name(sheet_name)[:31]
        sheets[safe_sheet] = sdf_clean

    xlsx_path, json_path = _write_excel_and_json(sheets, main_xlsx)
    print(f"  Saved main: {main_xlsx} ({len(group_df)} rows)")
    return xlsx_path, json_path, sheets


def _write_deep_split(group_df: pd.DataFrame, excel_dir: str) -> tuple:
    """
    max category depth > 3: one {cat2}.xlsx per level-2 subcategory,
    each split into one sheet per level-3 subcategory. Rows with no
    level-3 land in a single sheet named after the level-2 subcategory
    itself.
    """
    group_df = group_df.copy()
    group_df["_cat2"] = group_df["_names_en"].apply(lambda n: n[2] if len(n) > 2 else "Other")
    group_df["_cat3_group"] = group_df["_names_en"].apply(
        lambda n: n[3] if len(n) > 3 else (n[2] if len(n) > 2 else "Other")
    )

    excel_files = []
    json_files = []
    sheets = {}

    for cat2_name, c2_df in group_df.groupby("_cat2"):
        safe_cat2 = sanitize_name(cat2_name)
        cat2_xlsx = os.path.join(excel_dir, f"{safe_cat2}.xlsx")

        sub_sheets = {}
        for sheet_name, c3_df in c2_df.groupby("_cat3_group"):
            cols_to_drop = ["_cat2", "_cat3_group", "_cat0", "_cat1", "_names_en"]
            c3_clean = c3_df.drop(columns=[c for c in cols_to_drop if c in c3_df.columns])
            safe_sheet = sanitize_name(sheet_name)[:31]
            sub_sheets[safe_sheet] = c3_clean

        xlsx_path, json_path = _write_excel_and_json(sub_sheets, cat2_xlsx)
        excel_files.append(xlsx_path)
        json_files.append(json_path)
        print(f"    Saved: {cat2_xlsx} ({len(c2_df)} rows, {len(sub_sheets)} sheet(s))")

        sheets[safe_cat2] = c2_df

    return excel_files, json_files, sheets


def convert_timestamp_columns(df: pd.DataFrame) -> pd.DataFrame:
    timestamp_columns = [
        "added",
        "created_at",
        "last_updated_at"
    ]
    df = df.copy()
    for col in timestamp_columns:
        if col in df.columns:
            df[col] = (
                pd.to_datetime(
                    pd.to_numeric(df[col], errors="coerce"),
                    unit="s",
                    errors="coerce",
                    utc=True
                )
                .dt.tz_convert("Asia/Dubai")
                .dt.strftime("%Y-%m-%d %H:%M:%S")
            )

            print(f"  Converted timestamp column: {col}")

    return df


def _process_dataframe(df: pd.DataFrame, category_name: str, output_base_dir: str,
                        upload_images: bool, image_workers: int) -> dict:
    if df.empty:
        return {"excel_files": [], "json_files": []}

    df = df.copy()
    df["_names_en"] = df["category_v2"].apply(get_category_names)
    df["_cat0"] = df["_names_en"].apply(lambda n: n[0] if len(n) > 0 else "Unknown")

    df["_cat1"] = category_name

    if "id" in df.columns:
        df = df.drop_duplicates(subset=["id"], keep="first")

    excel_files = []
    json_files = []

    for (cat0, cat1), group_df in df.groupby(["_cat0", "_cat1"]):
        safe_cat0 = sanitize_name(cat0)
        safe_cat1 = sanitize_name(cat1)

        group_quality_report = generate_data_quality_report(group_df, len(group_df))

        cat_dir = os.path.join(output_base_dir, safe_cat0, safe_cat1)
        os.makedirs(cat_dir, exist_ok=True)

        if upload_images and "photo_thumbnails" in group_df.columns:
            print(f"  Processing images for {safe_cat0}/{safe_cat1} ({len(group_df)} products)...")
            group_df = process_images_for_group(
                group_df, category=category_name,
                cat0=safe_cat0, cat1=safe_cat1, workers=image_workers
            )

        excel_dir = os.path.join(cat_dir, "excel")
        json_dir = os.path.join(cat_dir, "json")
        summary_dir = os.path.join(cat_dir, "summary")
        os.makedirs(excel_dir, exist_ok=True)
        os.makedirs(json_dir, exist_ok=True)
        os.makedirs(summary_dir, exist_ok=True)

        max_depth = group_df["_names_en"].apply(len).max() if len(group_df) else 0
        if max_depth > 3:
            group_excel_files, group_json_files, sheets = _write_deep_split(group_df, excel_dir)
            excel_files.extend(group_excel_files)
            json_files.extend(group_json_files)
        else:
            xlsx_path, json_path, sheets = _write_shallow_split(group_df, excel_dir, safe_cat1)
            excel_files.append(xlsx_path)
            json_files.append(json_path)

        dt = datetime.now(timezone.utc)
        category_path = get_category_path(group_df["category_v2"].iloc[0]) if not group_df.empty and "category_v2" in group_df.columns else f"{safe_cat0}/{safe_cat1}"
        summary = build_group_summary(sheets, group_df, safe_cat0, safe_cat1, dt, output_base_dir, category_path)
        summary_file_path = os.path.join(summary_dir, "summary.json")
        with open(summary_file_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"  Saved summary: {summary_file_path} ({summary['total_subcategories']} subcats, {summary['total_listings']} listings)")

    return {"excel_files": excel_files, "json_files": json_files}


def process_category(category_name: str, jsonl_files: list, output_base_dir: str,
                      upload_images: bool = False, image_workers: int = 2,
                      enrich_description: bool = True) -> dict:
    df = load_all_hits(jsonl_files)

    if df.empty:
        return {"total": 0, "excel_files": [], "json_files": []}

    df = convert_timestamp_columns(df)

    if enrich_description and "absolute_url" in df.columns:
        print(f"  Enriching {len(df)} rows with description_full...")
        df = enrich_with_description(df)

    total = len(df)
    excel_files = []
    json_files = []

    result = _process_dataframe(df, category_name, output_base_dir, upload_images, image_workers)
    excel_files.extend(result["excel_files"])
    json_files.extend(result["json_files"])

    return {"total": total, "excel_files": excel_files, "json_files": json_files}