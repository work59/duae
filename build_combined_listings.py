import argparse
import glob
import json

import pandas as pd


def load_hits(jsonl_files: list) -> pd.DataFrame:
    rows = []
    for path in jsonl_files:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", type=str, required=True, help="glob pattern for raw jsonl files")
    parser.add_argument("--output", type=str, required=True)
    args = parser.parse_args()

    files = sorted(glob.glob(args.pattern))
    if not files:
        print(f"No files matched {args.pattern}, nothing to combine.")
        raise SystemExit(0)

    df = load_hits(files)

    if df.empty or "id" not in df.columns or "absolute_url" not in df.columns:
        print("No usable rows (missing id/absolute_url columns), skipping.")
        raise SystemExit(0)

    df = df[["id", "absolute_url"]].drop_duplicates(subset=["id"], keep="first")
    df.to_excel(args.output, index=False)
    print(f"Wrote {len(df)} unique listings to {args.output}")