import os
from dotenv import load_dotenv

load_dotenv()

VOYAGE_MODEL_GENERAL = os.getenv("VOYAGE_MODEL_GENERAL", "voyage-2")
VOYAGE_MODEL_LEGAL = os.getenv("VOYAGE_MODEL_LEGAL", "voyage-law-2")

DOCLING_BASE_URL = os.getenv("DOCLING_BASE_URL", "http://docling:5001")
DOCLING_TIMEOUT = float(os.getenv("DOCLING_TIMEOUT", "300"))
DOCLING_POLL_INTERVAL = float(os.getenv("DOCLING_POLL_INTERVAL", "5"))

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://localhost:4000/v1")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "")
LITELLM_TIMEOUT = float(os.getenv("LITELLM_TIMEOUT", "60"))

CHUNK_TOKENS = int(os.getenv("CHUNK_TOKENS", "500"))
OVERLAP_MAX_PCT = float(os.getenv("CHUNK_OVERLAP_MAX_PCT", "0.15"))

DATA_INBOX = os.getenv("DATA_INBOX", "./data/inbox")
DATA_PARSED = os.getenv("DATA_PARSED", "./data/parsed")

PGHOST = os.getenv("PGHOST", "localhost")
PGPORT = int(os.getenv("PGPORT", "5433"))
PGDATABASE = os.getenv("PGDATABASE", "paradedb")
PGUSER = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

def pg_dsn() -> str:
    return (
        f"host={PGHOST} port={PGPORT} dbname={PGDATABASE} "
        f"user={PGUSER} password={PGPASSWORD}"
    )
