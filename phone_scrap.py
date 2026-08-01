import ast
import os
import random
import time

import pandas as pd
from camoufox.sync_api import Camoufox

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


def _is_challenge_page(html: str) -> bool:
    return any(marker in html for marker in CHALLENGE_MARKERS)


def _safe_content(page, retries=3, delay=1500):
    for attempt in range(retries):
        try:
            return page.content()
        except Exception:
            if attempt == retries - 1:
                raise
            page.wait_for_timeout(delay)
    return ""


def _extract_en_url(raw_value):
    """
    absolute_url column can come in as:
      - an actual dict {'en': ..., 'ar': ...} (e.g. read from a pickle/parquet)
      - a stringified dict "{'en': ..., 'ar': ...}" (e.g. read from a csv)
      - a plain string url
    """
    if isinstance(raw_value, dict):
        return raw_value.get("en")

    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if stripped.startswith("{"):
            try:
                parsed = ast.literal_eval(stripped)
                if isinstance(parsed, dict):
                    return parsed.get("en")
            except Exception:
                pass
        return stripped  # plain url string

    return None


def _find_phone_recursive(obj):
    """
    Generic recursive search for any key containing 'phone' (case-insensitive)
    whose value looks like an actual phone number. Used because the exact
    graphql field name for a listing's phone hasn't been confirmed yet
    (unlike the agency endpoint, which is known to be contactPhoneNumber).
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if "phone" in key.lower() and isinstance(value, (str, int)) and value:
                return str(value)
        for value in obj.values():
            found = _find_phone_recursive(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_phone_recursive(item)
            if found:
                return found
    return None


def _reveal_listing_phone(page, timeout_ms=10000):
    captured = {"data": None}

    def handle_response(response):
        # e.g. https://dubai.dubizzle.com/m/api/v5/leads/{x}/{y}/listing-profile/
        if "listing-profile" not in response.url:
            return
        if response.status != 200:
            return
        try:
            captured["data"] = response.json()
        except Exception:
            pass

    page.on("response", handle_response)

    button = None
    for selector in BUTTON_SELECTORS:
        loc = page.locator(selector).first
        try:
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
    except Exception as e:
        page.remove_listener("response", handle_response)
        return None, f"click_error: {e}"
    finally:
        page.remove_listener("response", handle_response)

    if captured["data"] is None:
        return None, "no_response_captured"

    phone = _find_phone_recursive(captured["data"])
    if phone is None:
        return None, "phone_field_not_found"
    return phone, "ok"


def enrich_listings_with_phone(
    df: pd.DataFrame,
    url_column: str = "absolute_url",
    key_column: str = None,
    headless: bool = True,
    min_delay: float = 10,
    max_delay: float = 20,
    save_every: int = 25,
    checkpoint_path: str = "listings_with_phone_checkpoint.xlsx",
    resume: bool = True,
    max_new: int = None,
) -> pd.DataFrame:
    df = df.copy()

    # resolved english url per row, used both as the navigation target and
    # as the resume/checkpoint key (unless a dedicated key_column is given)
    df["_resolved_url"] = df[url_column].apply(_extract_en_url)
    key_col = key_column or "_resolved_url"

    already_done = {}  # key -> (phone, status)
    if resume and os.path.exists(checkpoint_path):
        try:
            prev_df = pd.read_excel(checkpoint_path)
            for _, prow in prev_df.iterrows():
                key = prow.get(key_col)
                status = prow.get("_scrape_status")
                if key and pd.notna(status) and status not in ("imperva_challenge", "button_not_found"):
                    already_done[key] = (prow.get("contact_phone_number"), status)
            print(f"Resuming: found {len(already_done)} already-processed listings in checkpoint.")
        except Exception as e:
            print(f"Could not read checkpoint for resume ({e}), starting fresh.")

    phones = [None] * len(df)
    statuses = [None] * len(df)

    for pos, (idx, row) in enumerate(df.iterrows()):
        key = row.get(key_col)
        if key in already_done:
            phones[pos], statuses[pos] = already_done[key]

    def save_checkpoint():
        partial_df = df.copy()
        partial_df["contact_phone_number"] = phones
        partial_df["_scrape_status"] = statuses
        partial_df.to_excel(checkpoint_path, index=False)

    new_processed_count = 0
    consecutive_soft_fails = 0
    SOFT_FAIL_THRESHOLD = 3

    with Camoufox(
        headless=headless,
        humanize=True,
        geoip=True,
        block_images=False,
    ) as browser:
        page = browser.new_page()

        for pos, (idx, row) in enumerate(df.iterrows()):
            key = row.get(key_col)
            url = row.get("_resolved_url")

            if key in already_done:
                continue

            if max_new is not None and new_processed_count >= max_new:
                print(f"Reached max_new limit ({max_new}), stopping this run.")
                break

            if not url:
                statuses[pos] = "no_url"
                print(f"[{pos + 1}/{len(df)}] Skipped - no url")
                continue

            print(f"[{pos + 1}/{len(df)}] {url}")

            attempt_result = None
            for attempt_num in range(2):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(random.uniform(6000, 1000))

                    html = _safe_content(page)
                    if _is_challenge_page(html):
                        print("Challenge detected")

                        page.screenshot(path="imperva.png", full_page=True)
                        with open("imperva.html", "w", encoding="utf-8") as f:
                            f.write(html)

                        print(page.url)
                        print(page.title())

                        input("Press Enter to close...")

                        statuses[pos] = "imperva_challenge"
                        save_checkpoint()
                        attempt_result = "imperva_break"
                        break

                    phone, status = _reveal_listing_phone(page)
                    phones[pos] = phone
                    statuses[pos] = status
                    print(f"  -> phone: {phone} (status: {status})")

                    if status in ("button_not_found", "phone_field_not_found"):
                        consecutive_soft_fails += 1
                        try:
                            safe_key = str(key).replace("/", "_")[:80]
                            page.screenshot(
                                path=f"debug_no_phone_{safe_key}.png", full_page=True
                            )
                            page_text = page.locator("body").inner_text()[:500]
                            print(f"     [DEBUG] Page title: {page.title()}")
                            print(f"     [DEBUG] Body text snippet: {page_text[:200]!r}")
                        except Exception as e:
                            print(f"     [DEBUG] Screenshot failed: {e}")
                    else:
                        consecutive_soft_fails = 0

                    attempt_result = "done"
                    break

                except Exception as e:
                    if attempt_num == 0:
                        print(f"  -> Transient error, retrying once: {e}")
                        page.wait_for_timeout(2000)
                        continue
                    statuses[pos] = f"error: {e}"
                    print(f"  -> FAILED after retry: {e}")
                    attempt_result = "failed"

            if attempt_result == "imperva_break":
                break

            if consecutive_soft_fails >= SOFT_FAIL_THRESHOLD:
                print(
                    f"\n⚠️  {consecutive_soft_fails} consecutive soft-fail results - "
                    "this looks like a soft rate-limit/reputation warning from Imperva, "
                    "not a real missing button. Stopping this run early to avoid a full block."
                )
                save_checkpoint()
                break

            new_processed_count += 1

            if new_processed_count % save_every == 0:
                save_checkpoint()
                print(f"  [Checkpoint saved - {new_processed_count} new rows this run]")

            if pos < len(df) - 1:
                time.sleep(random.uniform(min_delay, max_delay))

        page.close()

    save_checkpoint()
    print(f"\nThis run processed {new_processed_count} new listings.")

    df["contact_phone_number"] = phones
    df["_scrape_status"] = statuses
    return df


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=15)
    parser.add_argument("--input", type=str, default="used_cars.xlsx")
    parser.add_argument("--url-column", type=str, default="absolute_url")
    parser.add_argument("--key-column", type=str, default="id")
    parser.add_argument("--checkpoint-path", type=str, default="listings_checkpoint.xlsx")
    parser.add_argument("--output-prefix", type=str, default="listings_with_phone")
    args = parser.parse_args()

    if args.input.lower().endswith((".xlsx", ".xls")):
        listings_df = pd.read_excel(args.input)[args.start:args.end]
    else:
        listings_df = pd.read_csv(args.input)[args.start:args.end]

    result_df = enrich_listings_with_phone(
        listings_df,
        url_column=args.url_column,
        key_column=args.key_column,
        max_new=100,
        checkpoint_path=args.checkpoint_path,
    )

    result_df.to_csv(
        f"{args.output_prefix}_{args.start}_{args.end}.csv",
        index=False,
        encoding="utf-8-sig",
    )

    total = len(result_df)
    with_phone = result_df["contact_phone_number"].notna().sum()
    attempted = result_df["_scrape_status"].notna().sum()
    print(f"\nProgress so far: {attempted}/{total} attempted, {with_phone} have a phone number")