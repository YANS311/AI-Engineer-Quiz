"""Generate a synthetic demo catalog so a fresh install has content.

All sample media is generated on the fly (gradients, shapes, labelled
cards) so there is no bundled binary and nothing proprietary. Run with::

    python -m keepstack seed
"""
from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw, ImageFont

from . import ingest
from .auth import hash_password
from .audit import now_iso
from .db import get_conn, init_db, transaction

random.seed(7)  # deterministic demo set

_PALETTES = [
    ((94, 132, 247), (24, 28, 38)), ((70, 192, 138), (18, 30, 26)),
    ((242, 96, 122), (34, 18, 24)), ((220, 160, 60), (32, 26, 14)),
    ((150, 110, 220), (24, 18, 34)), ((60, 180, 200), (14, 28, 30)),
]
_SUBJECTS = [
    ("City skyline at dusk", ["architecture", "city", "skyline"]),
    ("Mountain trail map", ["map", "outdoors", "terrain"]),
    ("Quarterly report cover", ["document", "report", "finance"]),
    ("Public transit poster", ["poster", "transit", "civic"]),
    ("Coastal survey photo", ["coast", "survey", "environment"]),
    ("Heritage building facade", ["heritage", "architecture", "history"]),
    ("Park event banner", ["event", "park", "community"]),
    ("Census infographic", ["infographic", "data", "census"]),
    ("River watershed diagram", ["diagram", "water", "environment"]),
    ("Municipal logo mark", ["logo", "brand", "civic"]),
    ("Road maintenance notice", ["notice", "roads", "public-works"]),
    ("Library reading room", ["library", "interior", "culture"]),
]


def _make_image(idx: int, label: str) -> bytes:
    fg, bg = _PALETTES[idx % len(_PALETTES)]
    w, h = random.choice([(1200, 800), (800, 1200), (1000, 1000)])
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    # soft geometric composition
    for _ in range(6):
        x0, y0 = random.randint(-100, w), random.randint(-100, h)
        r = random.randint(120, 480)
        shade = tuple(min(255, c + random.randint(-30, 40)) for c in fg)
        draw.ellipse([x0, y0, x0 + r, y0 + r], fill=shade)
    draw.rectangle([0, h - 150, w, h], fill=(0, 0, 0))
    try:
        font = ImageFont.truetype("arial.ttf", 44)
    except Exception:
        font = ImageFont.load_default()
    # Centered so the caption survives the square centre-crop used by thumbnails.
    draw.text((w / 2, h - 75), label, font=font, fill=(255, 255, 255), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# Deterministic per-asset overrides: (idx, variant) -> (description, extra_tags, created_at)
_SCENARIO_OVERRIDES: dict[tuple[int, int], tuple[str, list[str], str]] = {
    # Environment & Survey collection: Harbor scenario test cases
    (4, 0): (
        'Harbor coastal survey photo — 港口航拍素材, 包含"一类航标"\n第二行备注：已审校',
        ["approved", "campaign"],
        "2026-07-10T12:00:00+00:00",
    ),
    (4, 1): (
        "Harbor shoreline draft survey (internal working copy)",
        ["approved", "campaign", "internal"],
        "2026-07-11T12:00:00+00:00",
    ),
    (8, 0): (
        "Harbor estuary and river watershed diagram, campaign edition",
        ["approved", "campaign"],
        "2026-07-10T12:00:00+00:00",  # deliberate timestamp tie with (4, 0)
    ),
    (8, 1): (
        "Harbor watershed baseline chart (approved archive only)",
        ["approved"],
        "2026-07-09T12:00:00+00:00",
    ),
    (1, 0): (
        'Harbor overlook and mountain trail map, "港口步道"与等高线图',
        ["approved", "campaign"],
        "2026-07-15T09:30:00+00:00",
    ),
    # Civic Communications collection: Harbor asset in a different collection
    (3, 0): (
        "Harbor ferry and public transit poster for summer campaign",
        ["approved", "campaign"],
        "2026-07-12T08:00:00+00:00",
    ),
    (3, 1): (
        "Transit route map (internal draft)",
        ["internal"],
        "2026-07-08T08:00:00+00:00",
    ),
}


def run_seed() -> dict:
    random.seed(7)
    init_db()
    conn = get_conn()
    # ensure admin exists (startup hook normally does this, but seed runs standalone)
    if not conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        conn.execute(
            "INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
            "VALUES ('admin','admin@example.org','Administrator',?, 'admin', 1, ?)",
            (hash_password("admin"), now_iso()))
        conn.commit()
    admin = dict(conn.execute("SELECT * FROM users WHERE username='admin'").fetchone())

    # demo collaborators
    for uname, role in [("curator", "editor"), ("contributor", "contributor"), ("viewer", "viewer")]:
        if not conn.execute("SELECT 1 FROM users WHERE username=?", (uname,)).fetchone():
            conn.execute(
                "INSERT INTO users (username, password_hash, role, is_active, created_at) VALUES (?,?,?,1,?)",
                (uname, hash_password("demo1234"), role, now_iso()))
    conn.commit()

    created = []
    print("Generating synthetic assets...")
    seq_no = 0
    for i, (label, tags) in enumerate(_SUBJECTS):
        for variant in range(2):  # two variants each -> 24 assets
            seq_no += 1
            data = _make_image(i * 2 + variant, f"{label} #{variant + 1}")
            asset = ingest.ingest_stream(io.BytesIO(data), f"{label.lower().replace(' ', '_')}_{variant+1}.jpg",
                                         user=admin, run_ai=True, title=label if variant == 0 else f"{label} (alt)")
            override = _SCENARIO_OVERRIDES.get((i, variant))
            if override:
                desc_text, extra_tags, ts = override
            else:
                desc_text = f"{label} catalog entry #{seq_no}"
                extra_tags = ["approved"] if variant == 0 else (["internal"] if i % 3 == 0 else ["approved", "campaign"])
                # Deliberate timestamp ties on 2026-07-10T12:00:00+00:00 for i in (0, 2) variant 0
                if variant == 0 and i in (0, 2):
                    ts = "2026-07-10T12:00:00+00:00"
                else:
                    ts = f"2026-07-{seq_no:02d}T10:00:00+00:00"

            conn.execute(
                "UPDATE assets SET description = ?, created_at = ?, updated_at = ? WHERE id = ?",
                (desc_text, ts, ts, asset["id"]),
            )
            for t in [*tags, *extra_tags]:
                conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
                tid = conn.execute("SELECT id FROM tags WHERE name=?", (t,)).fetchone()["id"]
                conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?, 'manual')",
                             (asset["id"], tid))
            conn.commit()
            ingest.reindex_fts(asset["id"])
            ingest.reindex_embedding(asset["id"])
            created.append(asset["id"])

    # collections
    import uuid as uuidlib
    cols = {}
    for name, desc in [("Civic Communications", "Posters, banners, notices for public outreach"),
                       ("Environment & Survey", "Coastal, watershed, and terrain imagery"),
                       ("Heritage & Culture", "Buildings, libraries, and cultural assets")]:
        existing_col = conn.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchone()
        if existing_col:
            cols[name] = existing_col["id"]
        else:
            cur = conn.execute(
                "INSERT INTO collections (uuid, name, description, kind, created_by, created_at) VALUES (?,?,?, 'folder', ?, ?)",
                (str(uuidlib.uuid4()), name, desc, admin["id"], now_iso()))
            cols[name] = cur.lastrowid
    conn.commit()
    # distribute assets across collections by tag affinity
    for aid in created:
        row = conn.execute(
            "SELECT GROUP_CONCAT(t.name) g FROM asset_tags at JOIN tags t ON t.id=at.tag_id WHERE at.asset_id=?",
            (aid,)).fetchone()
        g = (row["g"] or "")
        target = None
        if any(k in g for k in ("civic", "poster", "notice", "event", "logo", "transit", "roads")):
            target = cols["Civic Communications"]
        elif any(k in g for k in ("environment", "coast", "water", "survey", "terrain", "outdoors")):
            target = cols["Environment & Survey"]
        elif any(k in g for k in ("heritage", "history", "library", "culture")):
            target = cols["Heritage & Culture"]
        if target:
            conn.execute("INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) VALUES (?,?,?)",
                         (target, aid, now_iso()))
    conn.commit()

    print(f"Seeded {len(created)} assets, {len(cols)} collections, and demo users.")
    print("Sign in as admin/admin (also: curator, contributor, viewer / demo1234).")
    return {"assets": created, "collections": cols}


if __name__ == "__main__":
    run_seed()
