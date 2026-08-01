import logging
import openpyxl
import io
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


from monitor_r2 import (
    partition_date_for_listing,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")
#───────────────────────────────────────────────────────────────────────────────

def list_excel_files(client, bucket: str, prefix: str) -> List[Dict]:
    """
    Return all .xlsx objects under *prefix*.
    Each item: {key, size, last_modified}
    Handles pagination automatically.
    """
    results = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".xlsx"):
                results.append(
                    {
                        "key":           obj["Key"],
                        "size_bytes":    obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    }
                )
    return results

def download_excel(client, bucket: str, key: str) -> Optional[bytes]:
    """Download an .xlsx file from R2 and return raw bytes."""
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()
    except Exception as exc:
        log.warning(f"Could not download {key}: {exc}")
        return None


def inspect_excel(raw: bytes, file_key: str) -> Dict:
    """
    Open an xlsx from bytes and return:
      sheets: [{name, row_count, columns: [str]}]
      readable: bool
      error: str | None
    """
    result = {"file_key": file_key, "readable": False, "sheets": [], "error": None}
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            # Read header row
            headers = []
            rows_iter = ws.iter_rows(values_only=True)
            header_row = next(rows_iter, None)
            if header_row:
                headers = [str(c) for c in header_row if c is not None]
            # Count data rows (fast — don't load everything into memory)
            row_count = sum(1 for _ in rows_iter)
            result["sheets"].append(
                {"name": sheet_name, "row_count": row_count, "columns": headers}
            )
        wb.close()
        result["readable"] = True
    except Exception as exc:
        result["error"] = str(exc)
        log.warning(f"Error inspecting {file_key}: {exc}")
    return result

def accumulate_stats(
    existing: Dict,
    scraper_name: str,
    inspected: Dict,
    size_bytes: int,
    run_date: str,
) -> None:
    """
    Merge new observations into the *existing* stats dict (in-place).
    Structure:
      scraper_name:
        observed_dates: [...]
        file_size_kb: {min, max, last}
        sheets:
          sheet_name:
            row_count: {min, max, last}
            columns: [...]
    """
    entry = existing.setdefault(scraper_name, {
        "observed_dates":  [],
        "file_size_kb":    {"min": None, "max": None, "last": None},
        "sheets":          {},
    })

    if run_date not in entry["observed_dates"]:
        entry["observed_dates"].append(run_date)
        entry["observed_dates"] = sorted(entry["observed_dates"])[-30:]  # keep last 30

    kb = round(size_bytes / 1024, 1)
    fs = entry["file_size_kb"]
    fs["last"] = kb
    fs["min"]  = min(kb, fs["min"]) if fs["min"] is not None else kb
    fs["max"]  = max(kb, fs["max"]) if fs["max"] is not None else kb

    for sheet in inspected.get("sheets", []):
        sname = sheet["name"]
        se    = entry["sheets"].setdefault(sname, {
            "row_count": {"min": None, "max": None, "last": None},
            "columns":   [],
        })
        rc = sheet["row_count"]
        se["row_count"]["last"] = rc
        se["row_count"]["min"]  = min(rc, se["row_count"]["min"]) if se["row_count"]["min"] is not None else rc
        se["row_count"]["max"]  = max(rc, se["row_count"]["max"]) if se["row_count"]["max"] is not None else rc
        # Merge new columns (union)
        new_cols = [c for c in sheet["columns"] if c not in se["columns"]]
        se["columns"].extend(new_cols)

def r2_base_prefix(r2_path_raw: str) -> str:
    """Convert config r2_path like '{r2_bucket}/4sale-data/animals' to actual in-bucket prefix."""
    path = r2_path_raw.strip()
    if path.startswith("{"):
        path = path.split("/", 1)[1] if "/" in path else path
    return path.strip("/")

def partition_date_for_data_date(dt: datetime) -> datetime:
    """R2 folder uses save_date = listing date + 1 day (yesterday's listings → today's partition)."""
    return partition_date_for_listing(dt)


"""def excel_prefixes_for_date(base: str, dt: datetime) -> List[str]:
    # 
    # Build R2 date-partition prefixes for Excel discovery.
    # Tries zero-padded (month=06/day=09) and unpadded (month=6/day=9) forms.
    # 
    seen: set = set()
    prefixes: List[str] = []
    for month in (f"{dt.month:02d}", str(dt.month)):
        for day in (f"{dt.day:02d}", str(dt.day)):
            prefix = f"{base}/year={dt.year}/month={month}/day={day}/"
            if prefix not in seen:
                seen.add(prefix)
                prefixes.append(prefix)
    return prefixes"""

def excel_prefixes_for_date(base: str, dt: datetime) -> List[str]:
    """
    Build R2 date-partition prefixes for Excel discovery.
    
    If base = "DKSA/Mobile Phones & Accessories":
      - Tries: DKSA/Mobile Phones & Accessories/year=2026/month=07/day=23/excel/
      - Tries: DKSA/year=2026/month=07/day=23/Mobile Phones & Accessories/excel/
    """
    seen: set = set()
    prefixes: List[str] = []
    
    base = base.strip("/")
    parts = base.split("/")
    
    # Build date part: year=2026/month=07/day=23
    date_part = f"year={dt.year}/month={dt.month:02d}/day={dt.day:02d}"
    date_part_unpadded = f"year={dt.year}/month={dt.month}/day={dt.day}"
    
    for date_str in [date_part, date_part_unpadded]:
        # Option 1: base/date_part/excel/  (DKSA/Mobile Phones & Accessories/year=.../excel/)
        prefix1 = f"{base}/{date_str}/excel/"
        if prefix1 not in seen:
            seen.add(prefix1)
            prefixes.append(prefix1)
        
        # Option 2: prefix/date_part/subfolder/excel/  (DKSA/year=.../Mobile Phones & Accessories/excel/)
        if len(parts) > 1:
            prefix_path = "/".join(parts[:-1])  # "DKSA"
            subfolder = parts[-1]  # "Mobile Phones & Accessories"
            prefix2 = f"{prefix_path}/{date_str}/{subfolder}/excel/"
            if prefix2 not in seen:
                seen.add(prefix2)
                prefixes.append(prefix2)
        
        # Option 3: base/date_part/ (without excel) - for backwards compatibility
        prefix3 = f"{base}/{date_str}/"
        if prefix3 not in seen:
            seen.add(prefix3)
            prefixes.append(prefix3)
        
        # Option 4: prefix/date_part/subfolder/ (without excel)
        if len(parts) > 1:
            prefix4 = f"{prefix_path}/{date_str}/{subfolder}/"
            if prefix4 not in seen:
                seen.add(prefix4)
                prefixes.append(prefix4)
    
    return prefixes