# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import xml.etree.ElementTree as ET

import tomllib
from PIL import Image


@dataclass(frozen=True)
class Entry:
    page_png: Path
    pos: Tuple[int, int]
    size: Tuple[int, int]
    frame_offset: Tuple[int, int]
    frame_size: Tuple[int, int]
    pack_name: str
    page_name: str


def parse_pz_global_tsx(tsx_path: Path) -> List[str]:
    tree = ET.parse(tsx_path)
    root = tree.getroot()
    out: List[str] = []
    for tile_el in root.findall("tile"):
        props = tile_el.find("properties")
        if props is None:
            continue
        for prop in props.findall("property"):
            if prop.get("name") == "pz_name":
                v = prop.get("value")
                if v:
                    out.append(v)
                break
    return out


def split_tileset_local(pz_name: str) -> Tuple[str, str]:
    if "_" not in pz_name:
        return pz_name, ""
    a, b = pz_name.rsplit("_", 1)
    return a, b


def safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in name)


def run_unpack(pz_pack_tool: str, pack_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # If it already looks unpacked, skip
    any_toml = list(out_dir.rglob("*.toml"))
    any_png = list(out_dir.rglob("*.png"))
    if any_toml and any_png:
        return

    cmd = [pz_pack_tool, "unpack", str(pack_path), str(out_dir)]
    print("  ->", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr, file=sys.stderr)
        raise RuntimeError(f"pz-pack-tool failed unpacking: {pack_path.name}")


def load_entries_from_unpacked(pack_unpacked_dir: Path, pack_name: str) -> Dict[str, Entry]:
    """
    pz-pack-tool writes page PNGs and a TOML per page that lists entries.
    We'll scan for *.toml, assume each toml corresponds to a page PNG with same stem.
    """
    entries: Dict[str, Entry] = {}

    for toml_path in pack_unpacked_dir.rglob("*.toml"):
        page_name = toml_path.stem

        # Find page PNG (common patterns)
        candidates = [
            toml_path.with_suffix(".png"),
            toml_path.parent / f"{page_name}.png",
        ]
        page_png = None
        for c in candidates:
            if c.exists():
                page_png = c
                break
        if page_png is None:
            # Some unpackers nest things; try any png in same folder with same stem
            pngs = list(toml_path.parent.glob(f"{page_name}*.png"))
            if pngs:
                page_png = pngs[0]
        if page_png is None:
            continue

        data = toml_path.read_bytes()
        doc = tomllib.loads(data.decode("utf-8", errors="replace"))

        # Each top-level table is an entry name
        for entry_name, entry_data in doc.items():
            # Required
            pos = tuple(entry_data.get("pos", ()))  # [x,y]
            size = tuple(entry_data.get("size", ()))  # [w,h]
            if len(pos) != 2 or len(size) != 2:
                continue

            frame_offset = tuple(entry_data.get("frame_offset", (0, 0)))
            frame_size = tuple(entry_data.get("frame_size", size))

            entries[entry_name] = Entry(
                page_png=page_png,
                pos=(int(pos[0]), int(pos[1])),
                size=(int(size[0]), int(size[1])),
                frame_offset=(int(frame_offset[0]), int(frame_offset[1])),
                frame_size=(int(frame_size[0]), int(frame_size[1])),
                pack_name=pack_name,
                page_name=page_name,
            )

    return entries


def write_index(out_dir: Path, manifest_rows: List[dict]) -> None:
    html_path = out_dir / "index.html"
    items_js = []
    for r in manifest_rows:
        items_js.append({
            "pz_name": r["pz_name"],
            "tileset": r["tileset"],
            "local_id": r["local_id"],
            "thumb": r["thumb_rel"],
            "full": r["full_rel"],
            "pack": r["pack"],
            "page": r["page"],
        })

    html_doc = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>PZ Tile Browser</title>
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 16px; }
  .top { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
  input, select { padding:8px; font-size:14px; }
  .meta { color:#666; font-size:12px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap:10px; }
  .card { border:1px solid #ddd; border-radius:10px; padding:8px; background:#fff; }
  .card img { width:100%; height:auto; image-rendering: pixelated; border-radius:6px;
              background: repeating-conic-gradient(#f3f3f3 0% 25%, #ffffff 0% 50%) 50% / 20px 20px; }
  .name { font-size:12px; margin-top:6px; word-break: break-word; }
  a { text-decoration:none; color:inherit; }
</style>
</head>
<body>
<h2>PZ Tile Browser</h2>
<div class="top">
  <input id="q" placeholder="Search (e.g. walls_exterior_house_02)" size="40"/>
  <select id="tileset"></select>
  <select id="missing">
    <option value="all">All</option>
    <option value="only_missing">Only missing</option>
    <option value="only_found">Only found</option>
  </select>
  <div class="meta" id="counts"></div>
</div>
<div class="grid" id="grid"></div>

<script>
const ITEMS = __ITEMS__;
const tilesets = Array.from(new Set(ITEMS.map(x => x.tileset))).sort();
const sel = document.getElementById("tileset");
sel.innerHTML = '<option value="__ALL__">All tilesets</option>' + tilesets.map(t => `<option value="${t}">${t}</option>`).join("");

function render() {
  const q = document.getElementById("q").value.trim().toLowerCase();
  const ts = sel.value;
  const miss = document.getElementById("missing").value;

  let filtered = ITEMS.filter(it => {
    if (ts !== "__ALL__" && it.tileset !== ts) return false;
    if (q && !it.pz_name.toLowerCase().includes(q)) return false;
    const isMissing = (it.thumb === "" || it.full === "");
    if (miss === "only_missing" && !isMissing) return false;
    if (miss === "only_found" && isMissing) return false;
    return true;
  });

  document.getElementById("counts").textContent =
    `${filtered.length.toLocaleString()} shown / ${ITEMS.length.toLocaleString()} total`;

  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const it of filtered) {
    const div = document.createElement("div");
    div.className = "card";
    const a = document.createElement("a");
    a.href = it.full || "#";
    a.target = "_blank";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = it.pz_name;
    img.src = it.thumb || "";
    a.appendChild(img);
    div.appendChild(a);

    const nm = document.createElement("div");
    nm.className = "name";
    nm.textContent = it.pz_name;
    div.appendChild(nm);

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = (it.pack ? `${it.pack} - ${it.page}` : "MISSING");
    div.appendChild(meta);

    frag.appendChild(div);
  }
  grid.appendChild(frag);
}

document.getElementById("q").addEventListener("input", render);
document.getElementById("tileset").addEventListener("change", render);
document.getElementById("missing").addEventListener("change", render);
render();
</script>

</body>
</html>
""".replace("__ITEMS__", str(items_js).replace("'", '"'))

    html_path.write_text(html_doc, encoding="utf-8")
    print(f"Wrote HTML index: {html_path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tsx", required=True)
    ap.add_argument("--texturepacks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--thumb", type=int, default=64)
    ap.add_argument("--pz-pack-tool", default="pz-pack-tool", help="Path to pz-pack-tool exe (or in PATH)")
    args = ap.parse_args()

    tsx_path = Path(args.tsx)
    tex_dir = Path(args.texturepacks)
    out_dir = Path(args.out)

    if not tsx_path.exists():
        print(f"[ERROR] TSX not found: {tsx_path}", file=sys.stderr)
        return 2
    if not tex_dir.exists():
        print(f"[ERROR] texturepacks not found: {tex_dir}", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    full_dir = out_dir / "full"
    thumb_dir = out_dir / "thumbs"
    cache_dir = out_dir / "_unpacked_cache"
    full_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    print("[1/4] Reading pz_global.tsx...")
    pz_names = parse_pz_global_tsx(tsx_path)
    print(f"  -> {len(pz_names):,} tiles referenced")

    # These are the packs that usually contain world tiles.
    # Add more if you still see missing results.
    packs_to_unpack = [
        "Tiles2x.pack",
        "Tiles2x.floor.pack",
        "Tiles1x.pack",
        "Tiles1x.floor.pack",
    ]

    print("[2/4] Unpacking packs with pz-pack-tool...")
    all_entries: Dict[str, Entry] = {}
    page_cache: Dict[Path, Image.Image] = {}

    for pack_name in packs_to_unpack:
        pack_path = tex_dir / pack_name
        if not pack_path.exists():
            continue
        unpack_dir = cache_dir / pack_name
        print(f"Unpacking {pack_name} -> {unpack_dir}")
        run_unpack(args.pz_pack_tool, pack_path, unpack_dir)
        entries = load_entries_from_unpacked(unpack_dir, pack_name)
        print(f"  -> indexed {len(entries):,} entries from {pack_name}")
        # keep first occurrence
        for k, v in entries.items():
            if k not in all_entries:
                all_entries[k] = v

    print(f"  -> Total entries indexed: {len(all_entries):,}")

    print("[3/4] Cropping and writing images...")
    manifest: List[dict] = []
    missing = 0
    seen = set()

    for i, pz_name in enumerate(pz_names, 1):
        tileset, local_id = split_tileset_local(pz_name)
        if pz_name in seen:
            continue
        seen.add(pz_name)

        ent = all_entries.get(pz_name)
        if ent is None:
            missing += 1
            manifest.append({
                "pz_name": pz_name,
                "tileset": tileset,
                "local_id": local_id,
                "thumb_rel": "",
                "full_rel": "",
                "pack": "",
                "page": "",
            })
            continue

        # Load page atlas once
        if ent.page_png not in page_cache:
            page_cache[ent.page_png] = Image.open(ent.page_png).convert("RGBA")
        atlas = page_cache[ent.page_png]

        x, y = ent.pos
        w, h = ent.size
        crop = atlas.crop((x, y, x + w, y + h))

        out_name = safe_filename(pz_name) + ".png"
        full_path = full_dir / out_name
        thumb_path = thumb_dir / out_name

        crop.save(full_path)
        t = crop.copy()
        t.thumbnail((args.thumb, args.thumb), Image.NEAREST)
        t.save(thumb_path)

        manifest.append({
            "pz_name": pz_name,
            "tileset": tileset,
            "local_id": local_id,
            "thumb_rel": str(Path("thumbs") / out_name).replace("\\", "/"),
            "full_rel": str(Path("full") / out_name).replace("\\", "/"),
            "pack": ent.pack_name,
            "page": ent.page_name,
        })

        if i % 2000 == 0:
            print(f"  ...{i:,}/{len(pz_names):,} tiles")

    manifest_csv = out_dir / "tile_manifest.csv"
    with manifest_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pz_name", "tileset", "local_id", "thumb_rel", "full_rel", "pack", "page"])
        for r in manifest:
            w.writerow([r["pz_name"], r["tileset"], r["local_id"], r["thumb_rel"], r["full_rel"], r["pack"], r["page"]])

    print(f"  -> Wrote manifest: {manifest_csv}")
    print(f"  -> Missing tiles: {missing:,}")

    print("[4/4] Writing index.html...")
    write_index(out_dir, manifest)

    print("Done. Open:")
    print(out_dir / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())