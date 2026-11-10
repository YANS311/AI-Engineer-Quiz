"""Search and discovery.

Three complementary modes over one catalog:

  * keyword  - FTS5 full-text across title, description, tags, metadata
  * filter   - faceted structured filters (media type, tag, status, dates)
  * semantic - embedding cosine ranking ("natural language" / more-like-this)

Facet counts are returned alongside results so the UI can render a live,
drill-down sidebar.
"""
from __future__ import annotations

import csv
import io
from typing import Iterable, Optional

from . import ai
from .db import get_conn

SORTS = {
    "newest": "a.created_at DESC, a.id DESC",
    "created_at_desc": "a.created_at DESC, a.id DESC",
    "desc": "a.created_at DESC, a.id DESC",
    "oldest": "a.created_at ASC, a.id ASC",
    "created_at_asc": "a.created_at ASC, a.id ASC",
    "asc": "a.created_at ASC, a.id ASC",
    "name": "a.title ASC, a.id ASC",
    "largest": "a.size DESC, a.id DESC",
}


def _fts_query(q: str) -> str:
    """Turn user text into a safe FTS5 prefix query."""
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    return " ".join(f'"{t}"*' for t in terms)


def _normalize_tags(*sources: Optional[Iterable[str] | str]) -> list[str]:
    """Normalize tag inputs: split commas, strip, lowercase, drop blanks, deduplicate."""
    out: list[str] = []
    seen: set[str] = set()
    for src in sources:
        if not src:
            continue
        items = [src] if isinstance(src, str) else src
        for raw in items:
            if not raw:
                continue
            for part in str(raw).split(","):
                name = part.strip().lower()
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
    return out


def _build_query(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags: Optional[Iterable[str] | str] = None,
    exclude_tags: Optional[Iterable[str] | str] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
) -> tuple[str, str, list, str, bool]:
    """Build shared SQL fragments for asset search and export.

    Returns (join_sql, where_sql, params, order_sql, is_empty).
    """
    order = SORTS.get(sort, SORTS["newest"])
    inc_tags = _normalize_tags(tag, include_tags)
    exc_tags = _normalize_tags(exclude_tags)

    if set(inc_tags) & set(exc_tags):
        return "", "1 = 0", [], order, True

    where = ["a.status = ?"]
    params: list = [status]
    join = ""

    if q and q.strip():
        fts_q = _fts_query(q)
        if not fts_q:
            return "", "1 = 0", [], order, True
        join += " JOIN asset_fts f ON f.rowid = a.id"
        where.append("asset_fts MATCH ?")
        params.append(fts_q)

    if media_type and media_type.strip():
        where.append("a.media_type = ?")
        params.append(media_type.strip())

    if collection_id is not None:
        try:
            cid = int(collection_id)
        except (TypeError, ValueError):
            return "", "1 = 0", [], order, True
        join += " JOIN collection_assets ca ON ca.asset_id = a.id"
        where.append("ca.collection_id = ?")
        params.append(cid)

    if inc_tags:
        placeholders = ",".join("?" for _ in inc_tags)
        where.append(
            f"""a.id IN (
                SELECT at.asset_id FROM asset_tags at
                JOIN tags t ON t.id = at.tag_id
                WHERE t.name IN ({placeholders})
                GROUP BY at.asset_id
                HAVING COUNT(DISTINCT t.name) = ?
            )"""
        )
        params.extend(inc_tags)
        params.append(len(inc_tags))

    if exc_tags:
        placeholders = ",".join("?" for _ in exc_tags)
        where.append(
            f"""NOT EXISTS (
                SELECT 1 FROM asset_tags at
                JOIN tags t ON t.id = at.tag_id
                WHERE at.asset_id = a.id AND t.name IN ({placeholders})
            )"""
        )
        params.extend(exc_tags)

    return join, " AND ".join(where), params, order, False


def _tags_by_asset_ids(conn, asset_ids: list[int]) -> dict[int, list[str]]:
    if not asset_ids:
        return {}
    placeholders = ",".join("?" for _ in asset_ids)
    rows = conn.execute(
        f"""SELECT at.asset_id, t.name
            FROM asset_tags at
            JOIN tags t ON t.id = at.tag_id
            WHERE at.asset_id IN ({placeholders})
            ORDER BY t.name ASC""",
        asset_ids,
    ).fetchall()
    out: dict[int, list[str]] = {aid: [] for aid in asset_ids}
    for r in rows:
        out.setdefault(r["asset_id"], []).append(r["name"])
    return out


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags: Optional[Iterable[str] | str] = None,
    exclude_tags: Optional[Iterable[str] | str] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
    mode: str = "keyword",
    limit: int = 60,
    offset: int = 0,
) -> dict:
    conn = get_conn()

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 60
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    # Semantic mode ranks the candidate pool by embedding similarity.
    if mode == "semantic" and q and q.strip():
        return _semantic_search(q, media_type, status, limit, offset)

    join, where_sql, params, order, is_empty = _build_query(
        q=q,
        media_type=media_type,
        tag=tag,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        status=status,
        sort=sort,
    )

    if is_empty:
        return {
            "total": 0,
            "items": [],
            "facets": _facets(status),
            "mode": mode,
        }

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM assets a{join} WHERE {where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""SELECT a.* FROM assets a{join} WHERE {where_sql}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()

    row_dicts = [dict(r) for r in rows]
    tags_map = _tags_by_asset_ids(conn, [r["id"] for r in row_dicts])

    return {
        "total": total,
        "items": [_card(r, tags_map.get(r["id"], [])) for r in row_dicts],
        "facets": _facets(status),
        "mode": mode,
    }


def query_export_assets(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags: Optional[Iterable[str] | str] = None,
    exclude_tags: Optional[Iterable[str] | str] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
) -> list[dict]:
    """Return all matching assets in the same order as search(), without pagination."""
    conn = get_conn()
    join, where_sql, params, order, is_empty = _build_query(
        q=q,
        media_type=media_type,
        tag=tag,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        status=status,
        sort=sort,
    )
    if is_empty:
        return []

    rows = conn.execute(
        f"""SELECT a.* FROM assets a{join} WHERE {where_sql}
            ORDER BY {order}""",
        params,
    ).fetchall()

    row_dicts = [dict(r) for r in rows]
    tags_map = _tags_by_asset_ids(conn, [r["id"] for r in row_dicts])
    out = []
    for r in row_dicts:
        item = _card(r, tags_map.get(r["id"], []))
        out.append(item)
    return out


CSV_COLUMNS = [
    "uuid",
    "title",
    "description",
    "filename",
    "media_type",
    "tags",
    "created_at",
]


def export_csv(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags: Optional[Iterable[str] | str] = None,
    exclude_tags: Optional[Iterable[str] | str] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
) -> str:
    """Format all matching assets as RFC 4180 CSV text (UTF-8)."""
    items = query_export_assets(
        q=q,
        media_type=media_type,
        tag=tag,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        status=status,
        sort=sort,
    )
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for item in items:
        tags_str = ",".join(item.get("tags") or [])
        writer.writerow([
            item.get("uuid") or "",
            item.get("title") or "",
            item.get("description") or "",
            item.get("filename") or "",
            item.get("media_type") or "",
            tags_str,
            item.get("created_at") or "",
        ])
    return buf.getvalue()


def _semantic_search(q, media_type, status, limit, offset) -> dict:
    conn = get_conn()
    qvec = ai.embed(q)
    where = ["status = ?", "embedding IS NOT NULL"]
    params: list = [status]
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(where)}", params
    ).fetchall()
    scored = []
    for r in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(r["embedding"]))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    page = scored[offset:offset + limit]
    items = []
    for score, r in page:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        items.append(card)
    return {"total": len(scored), "items": items, "facets": _facets(status), "mode": "semantic"}


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    bvec = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [(ai.cosine(bvec, ai.unpack_embedding(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        out.append(card)
    return out


def _facets(status: str) -> dict:
    conn = get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = ? GROUP BY media_type ORDER BY c DESC",
        (status,),
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = ?
           GROUP BY t.name ORDER BY c DESC LIMIT 25""",
        (status,),
    ).fetchall()
    return {
        "media_types": [{"value": r["media_type"], "count": r["c"]} for r in media],
        "tags": [{"value": r["name"], "count": r["c"]} for r in tags],
    }


def _card(a: dict, tags: Optional[list[str]] = None) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": a["id"],
        "uuid": a["uuid"],
        "title": a["title"],
        "description": a.get("description"),
        "filename": a["filename"],
        "media_type": a["media_type"],
        "mime_type": a["mime_type"],
        "ext": a["ext"],
        "size": a["size"],
        "width": a["width"],
        "height": a["height"],
        "alt_text": a["alt_text"],
        "status": a["status"],
        "created_at": a["created_at"],
        "tags": tags if tags is not None else [],
    }
