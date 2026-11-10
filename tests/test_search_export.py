"""Automated tests for combined filtering, stable pagination, CSV export,
and post-mutation consistency.
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

os.environ.setdefault("KEEPSTACK_DATA_DIR", tempfile.mkdtemp(prefix="keepstack-test-"))
os.environ["KEEPSTACK_AI_ENABLED"] = "false"
os.environ["KEEPSTACK_OCR_ENABLED"] = "false"

from keepstack import db, ingest, search, seed  # noqa: E402
from keepstack.app import app  # noqa: E402
from keepstack.auth import hash_password, make_token  # noqa: E402
from keepstack.config import config  # noqa: E402


def _make_jpeg(color: tuple[int, int, int], size: tuple[int, int] = (240, 180)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture()
def isolated_env():
    """Provide a clean SQLite catalog and blob store per test."""
    old_dirs = (
        config.data_dir,
        config.db_path,
        config.blob_dir,
        config.thumb_dir,
        config.cache_dir,
    )
    old_conn = getattr(db._local, "conn", None)

    data_dir = Path(tempfile.mkdtemp(prefix="keepstack-search-test-"))
    config.data_dir = data_dir
    config.db_path = data_dir / "keepstack.db"
    config.blob_dir = data_dir / "blobs"
    config.thumb_dir = data_dir / "thumbnails"
    config.cache_dir = data_dir / "cache"
    db._local.conn = None

    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
        "VALUES ('admin', 'admin@example.org', 'Admin', ?, 'admin', 1, '2026-07-01T00:00:00+00:00')",
        (hash_password("admin"),),
    )
    conn.commit()

    try:
        yield conn
    finally:
        cur_conn = getattr(db._local, "conn", None)
        if cur_conn is not None:
            try:
                cur_conn.close()
            except Exception:
                pass
        (
            config.data_dir,
            config.db_path,
            config.blob_dir,
            config.thumb_dir,
            config.cache_dir,
        ) = old_dirs
        db._local.conn = old_conn
        shutil.rmtree(data_dir, ignore_errors=True)


def _auth_headers(user_id: int = 1, role: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(user_id, role)}"}


def _create_asset(
    conn,
    idx: int,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    collection_id: int | None = None,
    created_at: str = "2026-07-10T12:00:00+00:00",
    media_type: str = "image",
    status: str = "active",
) -> dict:
    if media_type == "image":
        raw = _make_jpeg(((idx * 37) % 255, (idx * 73) % 255, (idx * 113) % 255))
        filename = f"asset_{idx}.jpg"
    else:
        raw = f"Document content for asset {idx}: {title}".encode("utf-8")
        filename = f"asset_{idx}.pdf"

    asset = ingest.ingest_stream(
        io.BytesIO(raw),
        filename,
        run_ai=False,
        title=title,
    )
    conn.execute(
        "UPDATE assets SET description = ?, created_at = ?, updated_at = ?, media_type = ?, status = ? WHERE id = ?",
        (description, created_at, created_at, media_type, status, asset["id"]),
    )
    for t in tags or []:
        t_clean = t.strip().lower()
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t_clean,))
        tid = conn.execute("SELECT id FROM tags WHERE name = ?", (t_clean,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?, ?, 'manual')",
            (asset["id"], tid),
        )
    if collection_id is not None:
        conn.execute(
            "INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) VALUES (?, ?, ?)",
            (collection_id, asset["id"], created_at),
        )
    conn.commit()
    ingest.reindex_fts(asset["id"])
    return dict(conn.execute("SELECT * FROM assets WHERE id = ?", (asset["id"],)).fetchone())


def test_seed_reproducible_and_business_scenario(isolated_env):
    """Verify seed data generation and the README harbor business scenario."""
    summary = seed.run_seed()
    assert len(summary["assets"]) == 24
    assert len(summary["collections"]) == 3

    env_col_id = summary["collections"]["Environment & Survey"]
    client = TestClient(app)
    headers = _auth_headers()

    # Business scenario: in 'Environment & Survey', search 'harbor',
    # include 'approved' & 'campaign', exclude 'internal', sort by created_at.
    res_desc = client.get(
        "/api/assets",
        params=[
            ("q", "harbor"),
            ("collection_id", str(env_col_id)),
            ("include_tags", "approved"),
            ("include_tags", "campaign"),
            ("exclude_tags", "internal"),
            ("sort", "newest"),
        ],
        headers=headers,
    )
    assert res_desc.status_code == 200
    data_desc = res_desc.json()
    assert data_desc["total"] == 3
    titles_desc = [item["title"] for item in data_desc["items"]]
    assert titles_desc == [
        "Mountain trail map",
        "River watershed diagram",
        "Coastal survey photo",
    ]

    # Ascending created_at order should invert the sequence deterministically
    res_asc = client.get(
        "/api/assets",
        params=[
            ("q", "harbor"),
            ("collection_id", str(env_col_id)),
            ("include_tags", "approved"),
            ("include_tags", "campaign"),
            ("exclude_tags", "internal"),
            ("sort", "oldest"),
        ],
        headers=headers,
    )
    assert res_asc.status_code == 200
    titles_asc = [item["title"] for item in res_asc.json()["items"]]
    assert titles_asc == list(reversed(titles_desc))

    # CSV export for the exact same filter matches list items and order
    csv_res = client.get(
        "/api/assets/export",
        params=[
            ("q", "harbor"),
            ("collection_id", str(env_col_id)),
            ("include_tags", "approved"),
            ("include_tags", "campaign"),
            ("exclude_tags", "internal"),
            ("sort", "newest"),
        ],
        headers=headers,
    )
    assert csv_res.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_res.text)))
    assert [r["uuid"] for r in rows] == [item["uuid"] for item in data_desc["items"]]


def test_combined_filtering_semantics(isolated_env):
    """Test intersection of keyword, collection, multiple include/exclude tags, media_type, and status."""
    conn = isolated_env
    conn.execute(
        "INSERT INTO collections (id, uuid, name, kind, created_at) VALUES (1, 'col-1', 'Harbor Collection', 'folder', '2026-07-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO collections (id, uuid, name, kind, created_at) VALUES (2, 'col-2', 'Other Collection', 'folder', '2026-07-01T00:00:00Z')"
    )
    conn.commit()

    a1 = _create_asset(
        conn, 1,
        title="Harbor Pier Sunrise",
        description="Approved campaign harbor image",
        tags=["approved", "campaign", "featured"],
        collection_id=1,
        created_at="2026-07-05T10:00:00+00:00",
        media_type="image",
    )
    a2 = _create_asset(
        conn, 2,
        title="Harbor Dockyard",
        description="Has internal tag so must be excluded",
        tags=["approved", "campaign", "internal"],
        collection_id=1,
        created_at="2026-07-06T10:00:00+00:00",
        media_type="image",
    )
    a3 = _create_asset(
        conn, 3,
        title="Harbor Beacon",
        description="Missing campaign tag",
        tags=["approved"],
        collection_id=1,
        created_at="2026-07-07T10:00:00+00:00",
        media_type="image",
    )
    a4 = _create_asset(
        conn, 4,
        title="Harbor Regulations PDF",
        description="Document in Harbor Collection with both required tags",
        tags=["approved", "campaign"],
        collection_id=1,
        created_at="2026-07-08T10:00:00+00:00",
        media_type="document",
    )
    _create_asset(
        conn, 5,
        title="Harbor Outside Collection",
        description="In collection 2",
        tags=["approved", "campaign"],
        collection_id=2,
        created_at="2026-07-09T10:00:00+00:00",
        media_type="image",
    )
    _create_asset(
        conn, 6,
        title="Harbor Deleted Asset",
        description="Soft deleted asset",
        tags=["approved", "campaign"],
        collection_id=1,
        created_at="2026-07-10T10:00:00+00:00",
        media_type="image",
        status="deleted",
    )
    a7 = _create_asset(
        conn, 7,
        title="Untagged Harbor Sketch",
        description="No tags at all",
        tags=[],
        collection_id=1,
        created_at="2026-07-11T10:00:00+00:00",
        media_type="image",
    )

    # 1. Combined filter: q='harbor', collection=1, include=['approved', 'campaign'], exclude=['internal']
    res = search.search(
        q="harbor",
        collection_id=1,
        include_tags=["Approved", "CAMPAIGN", "approved"],  # case-insensitive + duplicate tag
        exclude_tags=["INTERNAL", "draft"],
        sort="oldest",
    )
    assert [i["uuid"] for i in res["items"]] == [a1["uuid"], a4["uuid"]]

    # 2. Adding media_type='image' narrows to a1 only
    res_img = search.search(
        q="harbor",
        collection_id=1,
        include_tags=["approved", "campaign"],
        exclude_tags=["internal"],
        media_type="image",
    )
    assert [i["uuid"] for i in res_img["items"]] == [a1["uuid"]]

    # 3. Partial tag name does NOT match ('approve' != 'approved')
    res_partial = search.search(include_tags=["approve"])
    assert res_partial["total"] == 0

    # 4. Same tag in both include_tags and exclude_tags returns empty
    res_conflict = search.search(
        include_tags=["approved", "campaign"],
        exclude_tags=["campaign"],
    )
    assert res_conflict["total"] == 0
    assert res_conflict["items"] == []

    # 5. Exclude tags keeps untagged assets when no include_tags are specified
    res_exc_only = search.search(collection_id=1, exclude_tags=["internal", "approved"])
    assert [i["uuid"] for i in res_exc_only["items"]] == [a7["uuid"]]
    assert a2["uuid"] not in [i["uuid"] for i in res_exc_only["items"]]
    assert a3["uuid"] not in [i["uuid"] for i in res_exc_only["items"]]

    # 6. Whitespace-only q does not restrict results; punctuation-only q returns 0 without error
    res_blank = search.search(q="   \t  ", collection_id=1)
    assert res_blank["total"] == 5  # a1, a2, a3, a4, a7 (a6 is deleted)

    res_special = search.search(q="!@#$%^&*()\"'---")
    assert res_special["total"] == 0
    assert res_special["items"] == []


def test_sorting_and_pagination_stability_with_identical_created_at(isolated_env):
    """Verify stable pagination without cross-page duplicates or omissions when created_at is identical."""
    conn = isolated_env
    same_ts = "2026-07-15T12:00:00+00:00"
    created_uuids = []
    for i in range(1, 8):
        a = _create_asset(
            conn, i,
            title=f"Harbor Tied Timestamp Asset {i}",
            description="harbor tied timestamp batch",
            tags=["approved"],
            created_at=same_ts,
        )
        created_uuids.append(a["uuid"])

    # Also add one older and one newer asset
    older = _create_asset(
        conn, 100,
        title="Harbor Older Asset",
        description="harbor tied timestamp batch",
        tags=["approved"],
        created_at="2026-07-01T00:00:00+00:00",
    )
    newer = _create_asset(
        conn, 101,
        title="Harbor Newer Asset",
        description="harbor tied timestamp batch",
        tags=["approved"],
        created_at="2026-07-20T00:00:00+00:00",
    )

    expected_newest = [newer["uuid"], *reversed(created_uuids), older["uuid"]]
    expected_oldest = [older["uuid"], *created_uuids, newer["uuid"]]

    # Paginate with limit=2 and keyword q='harbor' (must still obey sort!)
    for sort_key, expected in [("newest", expected_newest), ("oldest", expected_oldest)]:
        collected = []
        page_size = 2
        for offset in range(0, len(expected), page_size):
            page = search.search(q="harbor", sort=sort_key, limit=page_size, offset=offset)
            assert page["total"] == len(expected)
            page_uuids = [item["uuid"] for item in page["items"]]
            collected.extend(page_uuids)

        # No duplicates, no omissions, exact deterministic order
        assert len(collected) == len(set(collected)) == len(expected)
        assert collected == expected

    # Verify API validation on illegal pagination parameters
    client = TestClient(app)
    headers = _auth_headers()
    assert client.get("/api/assets?limit=0", headers=headers).status_code == 422
    assert client.get("/api/assets?limit=-5", headers=headers).status_code == 422
    assert client.get("/api/assets?offset=-1", headers=headers).status_code == 422
    assert client.get("/api/assets?limit=abc", headers=headers).status_code == 422

    # Out-of-range offset returns 200 with empty items and accurate total
    far_page = client.get("/api/assets?limit=5&offset=500", headers=headers)
    assert far_page.status_code == 200
    assert far_page.json()["total"] == 9
    assert far_page.json()["items"] == []


def test_csv_export_full_results_escaping_and_empty_header(isolated_env):
    """Verify CSV export requires auth, ignores page limit, escapes special chars, and outputs header when empty."""
    conn = isolated_env
    a1 = _create_asset(
        conn, 1,
        title='中文标题, 含逗号与"双引号"',
        description="第一行描述，含中文、逗号, 以及\"引号\"\n第二行描述：Harbor 港口",
        tags=["campaign", "approved"],
        created_at="2026-07-10T08:00:00+00:00",
    )
    a2 = _create_asset(
        conn, 2,
        title="Harbor Standard Asset",
        description="Second harbor item",
        tags=["approved", "campaign", " coastal "],
        created_at="2026-07-12T08:00:00+00:00",
    )
    _create_asset(
        conn, 3,
        title="Harbor Internal Asset",
        description="Excluded item",
        tags=["approved", "campaign", "internal"],
        created_at="2026-07-14T08:00:00+00:00",
    )

    client = TestClient(app)
    headers = _auth_headers()

    # 1. Unauthenticated request is rejected with 401
    unauth = client.get("/api/assets/export")
    assert unauth.status_code == 401

    # 2. Export returns all matching assets even if list view uses limit=1
    list_page1 = client.get(
        "/api/assets",
        params=[
            ("q", "harbor"),
            ("include_tags", "approved"),
            ("include_tags", "campaign"),
            ("exclude_tags", "internal"),
            ("sort", "newest"),
            ("limit", "1"),
            ("offset", "0"),
        ],
        headers=headers,
    ).json()
    assert list_page1["total"] == 2
    assert len(list_page1["items"]) == 1

    export_res = client.get(
        "/api/assets/export",
        params=[
            ("q", "harbor"),
            ("include_tags", "approved"),
            ("include_tags", "campaign"),
            ("exclude_tags", "internal"),
            ("sort", "newest"),
        ],
        headers=headers,
    )
    assert export_res.status_code == 200
    assert "text/csv" in export_res.headers["content-type"]

    reader = csv.DictReader(io.StringIO(export_res.text))
    assert reader.fieldnames == [
        "uuid",
        "title",
        "description",
        "filename",
        "media_type",
        "tags",
        "created_at",
    ]
    rows = list(reader)
    assert len(rows) == 2
    assert [r["uuid"] for r in rows] == [a2["uuid"], a1["uuid"]]

    # Verify exact preservation of Chinese, commas, quotes, and newlines
    row_a1 = rows[1]
    assert row_a1["title"] == '中文标题, 含逗号与"双引号"'
    assert row_a1["description"] == "第一行描述，含中文、逗号, 以及\"引号\"\n第二行描述：Harbor 港口"
    assert row_a1["tags"] == "approved,campaign"
    assert row_a1["media_type"] == "image"
    assert row_a1["created_at"] == "2026-07-10T08:00:00+00:00"

    # 3. Zero matching results still outputs the CSV header row
    empty_export = client.get(
        "/api/assets/export",
        params=[("q", "nonexistent_keyword_xyz")],
        headers=headers,
    )
    assert empty_export.status_code == 200
    empty_reader = csv.DictReader(io.StringIO(empty_export.text))
    assert empty_reader.fieldnames == [
        "uuid",
        "title",
        "description",
        "filename",
        "media_type",
        "tags",
        "created_at",
    ]
    assert list(empty_reader) == []


def test_updates_to_title_description_and_tags_reflect_in_search_and_export(isolated_env):
    """Verify editing title, description, or tags via existing endpoints updates search and CSV export."""
    conn = isolated_env
    asset = _create_asset(
        conn, 1,
        title="Initial Draft Title",
        description="Initial plain description",
        tags=["draft"],
        created_at="2026-07-10T10:00:00+00:00",
    )
    uuid = asset["uuid"]
    client = TestClient(app)
    headers = _auth_headers()

    # Initially does not match 'harbor' or tag 'approved'
    assert client.get("/api/assets", params={"q": "harbor"}, headers=headers).json()["total"] == 0

    # 1. Update title and description via PATCH /api/assets/{uuid}
    patch_res = client.patch(
        f"/api/assets/{uuid}",
        json={
            "title": "Updated Harbor Beacon",
            "description": '全新港口描述, 含"引号"\n以及换行',
        },
        headers=headers,
    )
    assert patch_res.status_code == 200

    # Now matches 'harbor' and updated description in both search and export
    search_after_patch = client.get("/api/assets", params={"q": "harbor"}, headers=headers).json()
    assert search_after_patch["total"] == 1
    assert search_after_patch["items"][0]["title"] == "Updated Harbor Beacon"

    # Old keyword 'Initial' no longer matches
    assert client.get("/api/assets", params={"q": "Initial"}, headers=headers).json()["total"] == 0

    # 2. Add tags 'approved' and 'campaign', remove 'draft'
    assert client.post(f"/api/assets/{uuid}/tags", json={"name": "Approved"}, headers=headers).status_code == 200
    assert client.post(f"/api/assets/{uuid}/tags", json={"name": "campaign"}, headers=headers).status_code == 200
    assert client.delete(f"/api/assets/{uuid}/tags/draft", headers=headers).status_code == 200

    # Filter with include_tags=['approved', 'campaign'] and exclude_tags=['draft']
    filtered = client.get(
        "/api/assets",
        params=[
            ("q", "harbor"),
            ("include_tags", "approved"),
            ("include_tags", "campaign"),
            ("exclude_tags", "draft"),
        ],
        headers=headers,
    ).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["uuid"] == uuid
    assert filtered["items"][0]["tags"] == ["approved", "campaign"]

    # CSV export reflects updated title, description, and tags
    csv_res = client.get(
        "/api/assets/export",
        params=[
            ("q", "harbor"),
            ("include_tags", "approved"),
            ("include_tags", "campaign"),
            ("exclude_tags", "draft"),
        ],
        headers=headers,
    )
    rows = list(csv.DictReader(io.StringIO(csv_res.text)))
    assert len(rows) == 1
    assert rows[0]["uuid"] == uuid
    assert rows[0]["title"] == "Updated Harbor Beacon"
    assert rows[0]["description"] == '全新港口描述, 含"引号"\n以及换行'
    assert rows[0]["tags"] == "approved,campaign"


def test_multipage_seed_pagination_matches_csv_export_and_query_formats(isolated_env):
    """Verify multi-page pagination over seeded data matches full CSV export and supports comma/empty params."""
    seed.run_seed()
    client = TestClient(app)
    headers = _auth_headers()

    # Paginate with limit=5 over all seeded assets with tag 'approved' excluding 'internal'
    page_size = 5
    all_paged_uuids = []
    offset = 0
    total = None

    while True:
        res = client.get(
            "/api/assets",
            params={
                "include_tags": "approved",
                "exclude_tags": "internal",
                "collection_id": "",  # empty string should mean no collection filter
                "sort": "newest",
                "limit": page_size,
                "offset": offset,
            },
            headers=headers,
        )
        assert res.status_code == 200
        payload = res.json()
        if total is None:
            total = payload["total"]
            assert total > page_size * 2  # spans at least 3 pages
        items = payload["items"]
        if not items:
            break
        all_paged_uuids.extend(item["uuid"] for item in items)
        offset += page_size

    assert len(all_paged_uuids) == total
    assert len(set(all_paged_uuids)) == total

    # Compare with full CSV export under identical filter & sort
    csv_res = client.get(
        "/api/assets/export",
        params={
            "include_tags": "approved",
            "exclude_tags": "internal",
            "collection_id": "",
            "sort": "newest",
        },
        headers=headers,
    )
    assert csv_res.status_code == 200
    csv_uuids = [r["uuid"] for r in csv.DictReader(io.StringIO(csv_res.text))]
    assert csv_uuids == all_paged_uuids

    # Invalid collection_id returns 422
    bad_col = client.get("/api/assets?collection_id=not_an_int", headers=headers)
    assert bad_col.status_code == 422
    bad_col_exp = client.get("/api/assets/export?collection_id=not_an_int", headers=headers)
    assert bad_col_exp.status_code == 422

