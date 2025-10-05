import glob
import os

from src.config import DATA_INBOX
from src.ingest_one import ingest


def run_batch(default_kind: str = "case_filing") -> None:
    pdfs = sorted(glob.glob(os.path.join(DATA_INBOX, "*.pdf")))
    if not pdfs:
        print(f"No PDFs found in {DATA_INBOX}")
        return
    for path in pdfs:
        print(f"Ingesting {path} ...")
        try:
            ingest(path, default_kind)
        except Exception as exc:
            print(f"[WARN] Failed to ingest {path}: {exc}")


if __name__ == "__main__":
    run_batch()

