import os
from datetime import datetime

import boto3
from dotenv import load_dotenv

load_dotenv()

CF_R2_ACCESS_KEY = os.getenv("CF_R2_ACCESS_KEY_ID")
CF_R2_SECRET_KEY = os.getenv("CF_R2_SECRET_ACCESS_KEY")
CF_R2_ENDPOINT_URL = os.getenv("CF_R2_ENDPOINT_URL")
BUCKET_NAME = os.getenv("CF_R2_BUCKET_NAME")

s3 = boto3.client(
    "s3",
    endpoint_url=CF_R2_ENDPOINT_URL,
    aws_access_key_id=CF_R2_ACCESS_KEY,
    aws_secret_access_key=CF_R2_SECRET_KEY,
    region_name="auto",
)
r2_prefix = "DUAE"

LOCAL_ROOT = f"{r2_prefix}_1"

today = datetime.utcnow()

YEAR = today.strftime("%Y")
MONTH = today.strftime("%m")
DAY = today.strftime("%d")
PREFIXES = [
    f"{r2_prefix}/year={YEAR}/month={MONTH}/day={DAY}/",
    f"{r2_prefix}/monitor/",
]

MONITOR_STATUS_FILE = f"{r2_prefix}/monitor/monitor_stats.yml"
MONITOR_CONFIG_FILE = f"{r2_prefix}/monitor/websites-config.yml"


def list_all_objects(prefix):
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=BUCKET_NAME,
        Prefix=prefix,
    ):
        for obj in page.get("Contents", []):
            yield obj["Key"]


def download_file(key):
    if "/images/" in key:
        return False

    local_path = os.path.join(LOCAL_ROOT, key)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    print(f"⬇ {key}")

    s3.download_file(
        BUCKET_NAME,
        key,
        local_path,
    )

    return True


def download_monitor_stats():
    local_path = os.path.join(LOCAL_ROOT, MONITOR_STATUS_FILE)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    print(f"⬇ {MONITOR_STATUS_FILE}")

    s3.download_file(
        BUCKET_NAME,
        MONITOR_STATUS_FILE,
        local_path,
    )

def download_monitor_config():
    local_path = os.path.join(LOCAL_ROOT, MONITOR_CONFIG_FILE)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    print(f"⬇ {MONITOR_CONFIG_FILE}")

    s3.download_file(
        BUCKET_NAME,
        MONITOR_CONFIG_FILE,
        local_path,
    )


def main():
    downloaded = 0
    skipped = 0

    for prefix in PREFIXES:
        print(f"\nSearching under: {prefix}")

        for key in list_all_objects(prefix):

            if key.endswith("/"):
                continue

            if "/images/" in key:
                skipped += 1
                continue

            try:
                if download_file(key):
                    downloaded += 1
            except Exception as e:
                print(f"❌ {key}")
                print(e)

    # Download monitor_status.yml
    try:
        download_monitor_stats()
        downloaded += 1
    except Exception as e:
        print(f"❌ {MONITOR_STATUS_FILE}")
        print(e)

    # Download websites-config.yml
        try:
            download_monitor_config()
            downloaded += 1
        except Exception as e:
            print(f"❌ {MONITOR_CONFIG_FILE}")
            print(e)


    print("\n==============================")
    print(f"Downloaded : {downloaded}")
    print(f"Skipped    : {skipped} (images)")
    print("==============================")


if __name__ == "__main__":
    main()