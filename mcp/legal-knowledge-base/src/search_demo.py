import sys

import psycopg

from src.config import VOYAGE_MODEL_LEGAL, pg_dsn
from src.embed_voyage import embed_query


def _vector_literal(values: list[float]) -> str:
    """Render a pgvector-compatible literal string."""

    return "[" + ",".join(f"{value:.10f}" for value in values) + "]"


def search(query: str, limit: int = 10) -> None:
    embedding = embed_query(query, VOYAGE_MODEL_LEGAL)
    as_vector = _vector_literal(embedding)
    dsn = pg_dsn()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT section,
                   idx,
                   doc_version,
                   version,
                   text,
                   emb_legal <=> %s::vector AS dist
            FROM chunks
            ORDER BY emb_legal <=> %s::vector
            LIMIT %s;
            """,
            (as_vector, as_vector, limit),
        )
        rows = cur.fetchall()
    for section, idx, doc_version, chunk_version, text, distance in rows:
        preview = text.replace("\n", " ")[:240]
        print(
            f"[{section} #{idx} | doc_v={doc_version} chunk_v={chunk_version}] dist={distance:.4f}\n{preview}\n"
        )


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "relocation under Florida law 61.13001"
    search(query)
