import uuid
from typing import Dict, Iterable, List, Optional, Tuple

import psycopg


def connect_db(dsn: str):
    return psycopg.connect(dsn)


def fetch_doc_versions(conn, slug: str) -> List[Tuple[str, int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, version, content_md FROM docs WHERE slug = %s ORDER BY version",
            (slug,),
        )
        return cur.fetchall()


def insert_doc_version(
    conn,
    slug: str,
    version: int,
    source_file: str,
    kind: str,
    content_md: str,
    token_count: int,
    emb_general,
    emb_legal,
) -> str:
    doc_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docs (
                id, slug, version, source_file, kind, content_md,
                token_count, emb_general, emb_legal
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id;
            """,
            (
                doc_id,
                slug,
                version,
                source_file,
                kind,
                content_md,
                token_count,
                emb_general,
                emb_legal,
            ),
        )
    conn.commit()
    return doc_id


def insert_chunks(conn, doc_id: str, doc_version: int, chunks: List[Dict]) -> None:
    rows: List[tuple] = []
    for chunk in chunks:
        chunk_id = chunk.get("id") or str(uuid.uuid4())
        chunk["id"] = chunk_id
        rows.append(
            (
                chunk_id,
                doc_id,
                doc_version,
                chunk.get("version", doc_version),
                chunk.get("title"),
                chunk.get("idx"),
                chunk.get("text"),
                chunk.get("page_from"),
                chunk.get("page_to"),
                chunk.get("token_count"),
                chunk.get("overlap", 0),
                chunk.get("emb_general"),
                chunk.get("emb_legal"),
            )
        )
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunks (
                id, doc_id, doc_version, version, section, idx, text, page_from, page_to,
                token_count, overlap_used, emb_general, emb_legal
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING;
            """,
            rows,
        )
    conn.commit()


def insert_tags(conn, table: str, parent_field: str, parent_id: str, tags: Iterable[Dict]):
    payload = [(parent_id, tag["tag_key"], tag["tag_value"]) for tag in tags]
    if not payload:
        return
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {table} ({parent_field}, tag_key, tag_value) VALUES (%s,%s,%s)",
            payload,
        )
    conn.commit()


def fetch_web_versions(conn, slug: str) -> List[Tuple[str, int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, version, content_md FROM web WHERE slug = %s ORDER BY version",
            (slug,),
        )
        return cur.fetchall()


def insert_web_version(
    conn,
    url: str,
    slug: str,
    version: int,
    title: Optional[str],
    content_md: str,
    token_count: int,
    emb_general,
    emb_legal,
) -> str:
    web_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO web (
                id, url, slug, version, title, content_md,
                token_count, emb_general, emb_legal
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id;
            """,
            (
                web_id,
                url,
                slug,
                version,
                title,
                content_md,
                token_count,
                emb_general,
                emb_legal,
            ),
        )
    conn.commit()
    return web_id


def insert_web_chunks(conn, web_id: str, web_version: int, chunks: List[Dict]) -> None:
    rows: List[tuple] = []
    for chunk in chunks:
        chunk_id = chunk.get("id") or str(uuid.uuid4())
        chunk["id"] = chunk_id
        rows.append(
            (
                chunk_id,
                web_id,
                web_version,
                chunk.get("version", web_version),
                chunk.get("title"),
                chunk.get("idx"),
                chunk.get("text"),
                chunk.get("token_count"),
                chunk.get("overlap", 0),
                chunk.get("emb_general"),
                chunk.get("emb_legal"),
            )
        )
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO web_chunks (
                id, web_id, web_version, version, section, idx, text,
                token_count, overlap_used, emb_general, emb_legal
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING;
            """,
            rows,
        )
    conn.commit()
