#!/usr/bin/env python3
import os, re, json, pathlib, html, sys
from datetime import datetime, timezone, date
from urllib.request import urlopen
import xml.etree.ElementTree as ET

FEED_URL = "https://media.rss.com/lytte/feed.xml"
CONTENT_ROOT = pathlib.Path("content/lytteovelser")
STATE_FILE = pathlib.Path("data/lytte_feed_index.json")

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = (text.replace("æ","ae").replace("ø","o").replace("å","a"))
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "episode"

def ensure_dirs():
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"seen": []}
    return {"seen": []}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def fetch_feed_xml(url: str) -> bytes:
    with urlopen(url, timeout=30) as r:
        return r.read()

def parse_date_rfc822(s: str) -> date:
    try:
        return datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z").date()
    except Exception:
        try:
            return datetime.fromisoformat(s).date()
        except Exception:
            return datetime.now(timezone.utc).date()

def text_or(elem, default=""):
    return elem.text if (elem is not None and elem.text is not None) else default

def strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>\s*<p>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()

def yml_escape_inline(s: str) -> str:
    return (s or "").replace('"','\\"')

def yml_block(s: str) -> str:
    # YAML block scalar for multi-line text
    s = (s or "").rstrip()
    if not s:
        return '""'
    lines = s.splitlines()
    indented = "\n".join(("  " + ln).rstrip() for ln in lines)
    return "|\n" + indented

def episode_id_from_link(link: str):
    m = re.search(r"/(\d+)/?$", link)
    return m.group(1) if m else None

def build_front_matter(item) -> dict:
    title = text_or(item.find("title")).strip()
    pub_dt = parse_date_rfc822(text_or(item.find("pubDate")))
    iso_date = pub_dt.isoformat()                # YYYY-MM-DD
    display_date = pub_dt.strftime("%d-%m-%Y")   # DD-MM-YYYY

    description_html = text_or(item.find("description"))
    description_clean = strip_html(description_html)
    link = text_or(item.find("link")).strip()

    eid = episode_id_from_link(link)
    rss_embed = ""
    if eid:
        rss_embed = (f'<iframe src="https://player.rss.com/lytte/{eid}" '
                     f'loading="lazy" style="width:100%;height:180px;border:0;overflow:hidden;" '
                     f'title="{title}"></iframe>')

    enclosure = item.find("enclosure")
    audio_url = enclosure.get("url").strip() if enclosure is not None and enclosure.get("url") else ""

    return {
        "title": title,
        "date": iso_date,
        "display_date": display_date,
        "description": description_clean,  # will be written as YAML block
        "rss_embed": rss_embed,
        "audio_url": audio_url,
        "link": link,
        "tags": ["lytteøvelser","podcast"],
        "draft": False
    }

FRONTMATTER_ORDER = ["title","date","display_date","description","rss_embed","audio_url","link","tags","draft"]

def render_front_matter(fm: dict) -> str:
    out = ["---"]
    for key in FRONTMATTER_ORDER:
        val = fm.get(key, "")
        if key == "tags":
            out.append('tags: ["lytteøvelser","podcast"]')
        elif key == "description":
            out.append("description: " + yml_block(val))
        elif isinstance(val, bool):
            out.append(f"{key}: {str(val).lower()}")
        else:
            out.append(f'{key}: "{yml_escape_inline(str(val))}"')
    out.append("---")
    return "\n".join(out) + "\n"

def standard_body(fm: dict) -> str:
    parts = []
    parts.append("{{< episodeplayer >}}")
    if fm.get("description"):
        parts.append("\n## Beskrivelse")
        parts.append(fm["description"])
    parts.append("\n## Notater\n- Viktige punkter:\n  - …\n  - …")
    parts.append("\n## Lenker")
    parts.append(f"- Original episode: {fm.get('link','')}")
    eid = episode_id_from_link(fm.get("link",""))
    if eid:
        parts.append(f"- Delbar spiller: https://player.rss.com/lytte/{eid}")
    return "\n".join(parts) + "\n"

def normalize_file(md_path: pathlib.Path, fm: dict) -> bool:
    """Normalize existing file: rebuild FM in our order + body backfill."""
    text = md_path.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, flags=re.DOTALL)
    if not m:
        # No/invalid FM: write fresh content, preserve old below
        old = text.strip()
        new = render_front_matter(fm) + standard_body(fm)
        if old:
            new += "\n---\n<!-- Tidligere innhold bevart under -->\n" + old + "\n"
        md_path.write_text(new, encoding="utf-8")
        return True

    # Rebuild FM from scratch in our canonical order
    body = m.group(2).strip()
    new_fm = render_front_matter(fm)

    # Ensure body has episodeplayer/sections
    if (not body) or ("episodeplayer" not in body):
        body = standard_body(fm)

    new = new_fm + body + ("\n" if not body.endswith("\n") else "")
    if new != text:
        md_path.write_text(new, encoding="utf-8")
        return True
    return False

def write_or_update_episode_file(fm: dict, slug: str) -> bool:
    ep_dir = CONTENT_ROOT / slug
    ep_dir.mkdir(parents=True, exist_ok=True)
    md_path = ep_dir / "index.md"

    if md_path.exists():
        return normalize_file(md_path, fm)

    # New file
    new = render_front_matter(fm) + standard_body(fm)
    md_path.write_text(new, encoding="utf-8")
    return True

def main():
    ensure_dirs()
    state = load_state()
    seen = set(state.get("seen", []))

    xml_bytes = fetch_feed_xml(FEED_URL)
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        print("Fant ikke <channel> i feeden.", file=sys.stderr)
        sys.exit(1)

    changed = 0
    for item in channel.findall("item"):
        guid = text_or(item.find("guid")) or text_or(item.find("link"))
        if not guid:
            continue
        fm = build_front_matter(item)
        slug = f"{fm['date']}-{slugify(fm['title'])}"
        updated = write_or_update_episode_file(fm, slug)
        if guid not in seen:
            seen.add(guid)
        if updated:
            changed += 1

    state["seen"] = sorted(seen)
    save_state(state)
    print(f"Ferdig. Normaliserte/oppdaterte filer: {changed}")

if __name__ == "__main__":
    main()