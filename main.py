import sys
import json
import time
import requests
import random
#import pandas as pd
from datetime import datetime, timezone, timedelta
from request_tracker import tracker

URL = "https://wd0ptz13zs-dsn.algolia.net/1/indexes/*/queries"

PARAMS = {
    "x-algolia-agent": "Algolia for JavaScript (4.24.0); Browser (lite)",
    "x-algolia-api-key": "cdd839b4fdac840289e88633779e8634",
    "x-algolia-application-id": "WD0PTZ13ZS",
}

CATEGORIES = {
    # ── Classifieds ──
    "electronics": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/electronics")',
    },
    "computers_networking": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/computers-networking")',
    },
    "business_industrial": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/business-industrial")',
    },
    "home_appliances": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/home-appliances")',
    },
    "sports_equipment": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/sports-equipment")',
    },
    "clothing_accessories": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/clothing-accessories")',
    },
    "cameras_imaging": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/cameras-imaging")',
    },
    "jewelry_watches": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/jewelry-watches")',
    },
    "pets": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/pets")',
    },
    "musical_instruments": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/musical-instruments")',
    },
    "gaming": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/gaming")',
    },
    "baby_items": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/baby-items")',
    },
    "toys": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/toys")',
    },
    "tickets_vouchers": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/tickets-vouchers")',
    },
    "collectibles": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/collectibles")',
    },
    "books": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/books")',
    },
    "music": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/music")',
    },
    "free_stuff": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/free-stuff")',
    },
    "lostfound": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/lostfound")',
    },
    "dvds_movies": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/dvds-movies")',
    },
    "mobile_phones_pdas": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/mobile-phones-pdas")',
    },
    "furniture_home_garden": {
        "index": "by_added_desc_classified.com",
        "filter": '("category_v2.slug_paths":"classified/furniture-home-garden")',
    },

    # ── Community ──
    "auto_services": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/auto-services")',
        "hits": []
    },
    "consultancy_services": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/consultancy-services")',
        "hits": []
    },
    "domestic": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/domestic")',
        "hits": []
    },
    "event_entertainment": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/event-entertainment")',
        "hits": []
    },
    "freelancers": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/freelancers")',
        "hits": []
    },
    "health_wellbeing_services": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/health-wellbeing-services")',
        "hits": []
    },
    "home_maintenance": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/home-maintenance")',
        "hits": []
    },
    "movers_removals": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/movers-removals")',
        "hits": []
    },
    "other_services": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/other-services")',
        "hits": []
    },
    "restoration_repairs": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/restoration-repairs")',
        "hits": []
    },
    "tutors_classes": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/tutors-classes")',
        "hits": []
    },
    "web_computer_services": {
        "index": "by_added_desc_community.com",
        "filter": '("category_v2.slug_paths":"community/web-computer-services")',
        "hits": []
    },
}

TARGET_DATE = datetime.now(timezone.utc).date() - timedelta(days=1)

def filter_yesterday_hits(hits):
    filtered = []

    for hit in hits:
        created_at = hit.get("created_at")

        if created_at is None:
            continue

        try:
            dt = datetime.fromtimestamp(int(created_at), tz=timezone.utc)

            if dt.date() == TARGET_DATE:
                filtered.append(hit)

        except (ValueError, TypeError):
            pass

    return filtered


def get_page_with_retry(category: dict, page: int, max_retries: int = 3) -> dict:
    payload = {
        "requests": [{
            "indexName": category["index"],
            "query": "",
            "params": f"page={page}&hitsPerPage=25&filters={category['filter']}",
        }]
    }

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(URL, params=PARAMS, json=payload, timeout=30)
            r.raise_for_status()
            tracker.log_request(source="product_detail", success=True)
            return r.json()
        except Exception as e:
            tracker.log_request(source="product_detail", success=False)
            print(f"  [Attempt {attempt}/{max_retries}] Page {page} failed: {e}")
            if attempt < max_retries:
                wait_time = attempt * 2
                print(f"  Waiting {wait_time}s before retry...")
                time.sleep(wait_time)

    return None


def run(category_name: str, start_page: int, end_page: int, output_jsonl: str) -> dict:
    if category_name not in CATEGORIES:
        print(f"Unknown category: {category_name}")
        return {"success": 0, "failed": 0, "failed_pages": [], "total_pages": 0}

    category = CATEGORIES[category_name]
    print(f"Scraping {category_name} | pages {start_page}-{end_page}")

    hits = []
    failed_pages = []
    total_pages = end_page - start_page + 1

    for page in range(start_page, end_page + 1):
        print(f"  Processing page {page}...")

        data = get_page_with_retry(category, page, max_retries=3)

        if data is None:
            print(f"  [FAILED] Page {page} failed after 3 attempts, skipping...")
            failed_pages.append(page)
            continue

        try:
            # page_hits = data["results"][0]["hits"]
            # print(f"  Page {page}: {len(page_hits)} listings")

            # if not page_hits:
            #     print(f"  Page {page} has no results, stopping...")
            #     break

            # hits.extend(page_hits)

            page_hits = data["results"][0]["hits"]
            if not page_hits:
                print(f"  Page {page} has no results, stopping...")
                break
            filtered_hits = filter_yesterday_hits(page_hits)
            print(
                f"  Page {page}: {len(page_hits)} listings "
                f"-> kept {len(filtered_hits)}"
            )
            hits.extend(filtered_hits)
            delay = random.uniform(0.5, 2.5)
            print(f"  Waiting {delay:.2f}s before next request...")
            time.sleep(delay)

        except Exception as e:
            print(f"  [ERROR] Page {page} data processing failed: {e}")
            failed_pages.append(page)

    with open(output_jsonl, "w", encoding="utf-8") as f:
        for hit in hits:
            f.write(json.dumps(hit, ensure_ascii=False) + "\n")
    # df = pd.DataFrame(hits)
    # df.to_csv('output.csv', index=False)

    if failed_pages:
        failed_file = output_jsonl.replace(".jsonl", "_failed.txt")
        with open(failed_file, "w", encoding="utf-8") as f:
            f.write(f"Category: {category_name}\n")
            f.write(f"Total pages in this job: {total_pages}\n")
            f.write(f"Failed pages: {len(failed_pages)}\n")
            f.write(f"Failed percentage: {(len(failed_pages)/total_pages)*100:.2f}%\n\n")
            for p in failed_pages:
                f.write(f"page={p}\n")

    stats_file = f"request_stats_{start_page}_{end_page}.json"
    stats = tracker.save(stats_file)

    print(f"\n--- Combined Request Stats ---")
    print(f"Total: {stats['total_requests']} req | {stats['total_req_per_min']} req/min")
    print(f"By source: {stats['per_source']}")
    for worker, s in stats["per_worker"].items():
        print(f"  {worker}: {s['requests']} req | {s['req_per_min']} req/min")

    print(f"Saved {len(hits)} listings to {output_jsonl} | {len(failed_pages)} failed pages")

    return {
        "success": len(hits),
        "failed": len(failed_pages),
        "failed_pages": failed_pages,
        "total_pages": total_pages
    }


if __name__ == "__main__":
    if len(sys.argv) == 4:
        category_name = sys.argv[1]
        start = int(sys.argv[2])
        end = int(sys.argv[3])
        output = f"{category_name}_{start}_{end}.jsonl"
        result = run(category_name, start, end, output)

        result_file = f"{category_name}_{start}_{end}_result.json"
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({
                **result,
                "category": category_name,
                "start_page": start,
                "end_page": end,
                "timestamp": datetime.now().isoformat()
            }, f, indent=2)
    else:
        print("Usage: python main.py <category_name> <start_page> <end_page>")
        sys.exit(1)