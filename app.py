import os
import json
import uuid
import tempfile
import asyncio
import hashlib

import anthropic
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Page Guide")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_frontend(request, call_next):
    """Force the browser to always revalidate index.html/script.js/style.css
    with the server instead of serving a stale cached copy after a deploy."""
    response = await call_next(request)
    if request.url.path in ("/", "/index.html", "/script.js", "/style.css"):
        response.headers["Cache-Control"] = "no-cache"
    return response


client = anthropic.Anthropic()
# Async client used specifically for /explain's long-lived streaming call, so
# it can be cancelled the moment the client disconnects instead of running to
# completion regardless — see the disconnect check inside generate() below.
async_client = anthropic.AsyncAnthropic()
content_store: dict = {}


# ── Reading progress (cross-device "resume where I left off") ──────────────────
# Identifies a book by a hash of its own content (file bytes, or the URL for a
# web link), not by any user account — there's no login system here. Entirely
# optional: if DATABASE_URL isn't set, or the database is briefly unreachable,
# every function here just no-ops so the app still works fully, just without
# remembering position.

DATABASE_URL = os.getenv("DATABASE_URL", "")


def get_db_connection():
    if not DATABASE_URL:
        return None
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def ensure_progress_table():
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reading_progress (
                    content_hash TEXT PRIMARY KEY,
                    title TEXT,
                    page_index INTEGER NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                ALTER TABLE reading_progress
                ADD COLUMN IF NOT EXISTS glossary JSONB NOT NULL DEFAULT '{}'::jsonb
            """)
            # Library support: the file itself (or, for a plain web article,
            # just the URL to re-fetch it from), so a saved book can be
            # reopened without the user re-uploading it. page_index has to
            # become nullable — a library entry saved at upload time, before
            # any page has actually been read, means "no resume position
            # yet," which must stay distinct from "resume at page 0."
            cur.execute("ALTER TABLE reading_progress ALTER COLUMN page_index DROP NOT NULL")
            cur.execute("ALTER TABLE reading_progress ADD COLUMN IF NOT EXISTS filename TEXT")
            cur.execute("ALTER TABLE reading_progress ADD COLUMN IF NOT EXISTS suffix TEXT")
            cur.execute("ALTER TABLE reading_progress ADD COLUMN IF NOT EXISTS file_bytes BYTEA")
            cur.execute("ALTER TABLE reading_progress ADD COLUMN IF NOT EXISTS original_url TEXT")
            cur.execute("ALTER TABLE reading_progress ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN NOT NULL DEFAULT FALSE")
    except Exception:
        pass
    finally:
        conn.close()


ensure_progress_table()

# A saved file larger than this just doesn't get added to the library —
# the current reading session still works fine, it just won't show up in
# "Your Books" afterward. Keeps one big upload from eating the whole
# shared database storage budget.
MAX_LIBRARY_FILE_BYTES = 25 * 1024 * 1024


def save_file_to_library(content_hash: str, title: str, filename: str | None,
                          suffix: str | None, file_bytes: bytes | None, original_url: str | None) -> bool:
    """Best-effort, like save_progress — never blocks or breaks the upload
    itself if the database is unavailable or the file's too large."""
    if file_bytes is not None and len(file_bytes) > MAX_LIBRARY_FILE_BYTES:
        return False
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO reading_progress (content_hash, title, filename, suffix, file_bytes, original_url, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (content_hash) DO UPDATE
                SET title = EXCLUDED.title, filename = EXCLUDED.filename, suffix = EXCLUDED.suffix,
                    file_bytes = EXCLUDED.file_bytes, original_url = EXCLUDED.original_url, updated_at = now()
            """, (content_hash, title, filename, suffix, file_bytes, original_url))
        return True
    except Exception:
        return False
    finally:
        conn.close()


def list_library_books():
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT content_hash, title, filename, suffix, original_url, page_index, is_favorite, updated_at
                FROM reading_progress
                WHERE file_bytes IS NOT NULL OR original_url IS NOT NULL
                ORDER BY is_favorite DESC, updated_at DESC
            """)
            rows = cur.fetchall()
            return [
                {
                    "content_hash": r[0],
                    "title": r[1] or r[2] or "Untitled",
                    "filename": r[2],
                    "suffix": r[3],
                    "original_url": r[4],
                    "page_index": r[5],
                    "is_favorite": r[6],
                    "updated_at": r[7].isoformat() if r[7] else None,
                }
                for r in rows
            ]
    except Exception:
        return []
    finally:
        conn.close()


def load_library_file(content_hash: str):
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT title, filename, suffix, file_bytes, original_url "
                "FROM reading_progress WHERE content_hash = %s",
                (content_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "title": row[0], "filename": row[1], "suffix": row[2],
                "file_bytes": bytes(row[3]) if row[3] is not None else None,
                "original_url": row[4],
            }
    except Exception:
        return None
    finally:
        conn.close()


def set_favorite_book(content_hash: str) -> bool:
    """Only one favorite at a time — picking a new one un-favorites the rest."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE reading_progress SET is_favorite = FALSE WHERE content_hash != %s", (content_hash,))
            cur.execute("UPDATE reading_progress SET is_favorite = TRUE WHERE content_hash = %s", (content_hash,))
            return cur.rowcount > 0
    except Exception:
        return False
    finally:
        conn.close()


def get_favorite_book():
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM reading_progress WHERE is_favorite = TRUE LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def get_saved_progress(content_hash: str):
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT page_index, glossary FROM reading_progress WHERE content_hash = %s",
                (content_hash,),
            )
            row = cur.fetchone()
            return {"page_index": row[0], "glossary": row[1] or {}} if row else None
    except Exception:
        return None
    finally:
        conn.close()


def save_progress(content_hash: str, title: str, page_index: int, glossary: dict) -> bool:
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO reading_progress (content_hash, title, page_index, glossary, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (content_hash) DO UPDATE
                SET page_index = EXCLUDED.page_index, glossary = EXCLUDED.glossary,
                    updated_at = now(), title = EXCLUDED.title
            """, (content_hash, title, page_index, json.dumps(glossary)))
        return True
    except Exception:
        return False
    finally:
        conn.close()


# ── Extraction ────────────────────────────────────────────────────────────────

def extract_comic_pages(file_path: str) -> list[dict]:
    """Extract pages from a CBR or CBZ comic book file as base64 images."""
    import base64
    import zipfile
    from PIL import Image
    import io

    ext = os.path.splitext(file_path)[1].lower()
    image_bytes_list = []
    IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

    if ext == ".cbz":
        with zipfile.ZipFile(file_path) as zf:
            names = sorted([
                n for n in zf.namelist()
                if n.lower().endswith(IMAGE_EXTS)
                and not os.path.basename(n).startswith(".")
            ])
            for name in names:
                image_bytes_list.append(zf.read(name))

    elif ext == ".cbr":
        raise Exception(
            "CBR not supported here — please convert to CBZ using Calibre (free) and re-upload."
        )

    pages = []
    for i, raw_bytes in enumerate(image_bytes_list):
        try:
            img = Image.open(io.BytesIO(raw_bytes))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            # Resize very large pages to keep memory reasonable
            max_dim = 1800
            if max(img.width, img.height) > max_dim:
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=82)
            final_bytes = buf.getvalue()
        except Exception:
            continue  # skip unreadable images

        pages.append({
            "index": len(pages),
            "page_num": i + 1,
            "title": f"Page {i + 1}",
            "text": "",
            "image_data": base64.b64encode(final_bytes).decode(),
            "media_type": "image/jpeg",
        })

    return pages


def _pdf_pages_from_doc(doc) -> list[dict]:
    pages = []
    for i in range(len(doc)):
        text = doc[i].get_text().strip()
        if len(text) > 80:
            pages.append({
                "index": len(pages),
                "page_num": i + 1,
                "title": f"Page {i + 1}",
                "text": text[:50000],
            })
    return pages


def extract_pdf_pages(file_path: str) -> list[dict]:
    import fitz
    doc = fitz.open(file_path)
    try:
        return _pdf_pages_from_doc(doc)
    finally:
        doc.close()


def extract_pdf_pages_from_bytes(pdf_bytes: bytes) -> list[dict]:
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return _pdf_pages_from_doc(doc)
    finally:
        doc.close()


def render_pdf_page_image(pdf_bytes: bytes, page_num: int) -> bytes:
    """Rasterize a single PDF page (1-indexed) to a JPEG image, on demand."""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pix = doc[page_num - 1].get_pixmap(matrix=fitz.Matrix(2, 2))
        return pix.tobytes("jpeg")
    finally:
        doc.close()


def extract_text_pages(content: str, words_per_page: int = 300) -> list[dict]:
    words = content.split()
    pages = []
    for i in range(0, len(words), words_per_page):
        chunk = " ".join(words[i:i + words_per_page])
        if len(chunk.strip()) > 80:
            page_num = len(pages) + 1
            pages.append({
                "index": len(pages),
                "page_num": page_num,
                "title": f"Page {page_num}",
                "text": chunk,
            })
    return pages


def validate_public_url(url: str) -> None:
    """Reject anything that isn't a plain http(s) link to a public address."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Please enter a valid http:// or https:// link.")

    try:
        addrs = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise ValueError("Could not resolve that link's address.")

    for family, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise ValueError("That link isn't a public web address.")


def fetch_url_content(url: str) -> dict:
    """Fetch a link and return pages from it — handles both web pages and direct PDF links."""
    import requests as req_lib
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse

    resp = req_lib.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        timeout=20,
        allow_redirects=True,
    )
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "").lower()

    if "pdf" in content_type or urlparse(url).path.lower().endswith(".pdf"):
        pdf_bytes = resp.content
        pages = extract_pdf_pages_from_bytes(pdf_bytes)
        title = os.path.basename(urlparse(url).path) or url
        return {"title": title, "pages": pages, "pdf_bytes": pdf_bytes, "has_page_images": True}

    if "html" not in content_type:
        raise ValueError("That link isn't a web page or PDF I can read.")

    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else url

    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg"]):
        tag.decompose()

    article = soup.find("article") or soup.find("main") or soup.body or soup
    lines = [line.strip() for line in article.get_text(separator="\n").splitlines()]
    text = "\n".join(line for line in lines if line)

    return {"title": title, "pages": extract_text_pages(text), "pdf_bytes": None, "has_page_images": False}


LANGUAGE_NAMES = {"english": "English", "spanish": "Spanish"}


def normalize_language(value: str) -> str:
    value = (value or "").strip().lower()
    return value if value in LANGUAGE_NAMES else "english"


def generate_book_overview(pages: list[dict], language: str = "english") -> str:
    """Quick one-time call on upload to understand what the book is about."""
    is_comic = bool(pages and pages[0].get("image_data"))
    lang_name = LANGUAGE_NAMES[normalize_language(language)]

    if is_comic:
        # Send first few pages as images
        content = []
        for p in pages[:6]:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": p["media_type"],
                    "data": p["image_data"],
                },
            })
        content.append({
            "type": "text",
            "text": (
                "These are the first few pages of a comic book. "
                "Write 3-4 casual sentences: what is this comic about, who are the main "
                "characters, and what's the central story or conflict? "
                f"Write in plain, casual {lang_name}. No markdown. No bullet points. No headers. No bold. Just plain sentences."
            ),
        })
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": content}],
        )
    else:
        sample = "\n\n---\n\n".join(
            f'{p["title"]}:\n{p["text"][:1500]}'
            for p in pages[:8]
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{
                "role": "user",
                "content": (
                    f"Here are the first few pages of a book:\n\n{sample}\n\n"
                    "Write 3-4 casual sentences: what is this book about, who are the main "
                    "characters, and what central conflict or question is being set up? "
                    f"Write in plain, casual {lang_name}. No markdown. No bullet points. No headers. No bold. Just plain sentences."
                ),
            }],
        )
    return msg.content[0].text.strip()


def classify_front_matter(pages: list[dict]) -> set[int]:
    """Identify leading pages that are publisher/reference front matter with
    no real content to explain — copyright notices, ISBN/printing info, and
    "also by this author" / other-books-in-series listings — scoped to only
    the first handful of pages, where front matter actually lives, to limit
    how much a misclassification could affect. Returns a set of page
    `index` values; callers mark those "skip" but keep them in the page
    list so they're still reachable via Jump to Page."""
    candidates = [p for p in pages[:15] if not p.get("image_data")]
    if not candidates:
        return set()

    numbered = "\n\n---\n\n".join(
        f'Page {p["index"]}:\n{p["text"][:800]}' for p in candidates
    )
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Here are the first few pages of a book:\n\n{numbered}\n\n"
                    "Which of these pages are pure publisher/reference front matter, "
                    "with nothing a reader would actually want explained? That "
                    "includes: a copyright notice, ISBN, 'all rights reserved,' "
                    "printing/edition history, publisher imprint information, and "
                    "a list of the author's other books or other books in the "
                    "series ('Also by...', 'Books by this author', etc.).\n\n"
                    "Do NOT include a title page with just the book/author name, a "
                    "dedication, a table of contents, or anything with real written "
                    "content — personal introductions, forewords, author's notes, "
                    "and prologues all count as real content and must NOT be listed, "
                    "even if they mention the publisher in passing.\n\n"
                    "Reply with ONLY a comma-separated list of the page numbers to "
                    "skip (using the numbers above), or 'none' if none qualify. "
                    "No explanation, nothing else."
                ),
            }],
        )
        reply = msg.content[0].text.strip()
        import re
        found = {int(n) for n in re.findall(r"\d+", reply)}
        valid_indices = {p["index"] for p in candidates}
        return found & valid_indices
    except Exception:
        return set()


def update_glossary(glossary: dict, page: dict, language: str) -> dict:
    """Extract/update invented names, terms, and concepts from a page so they
    can be reminded to the reader whenever they resurface much later in the
    book — this is what makes the glossary useful past page 100, not just a
    one-time definition that gets forgotten."""
    lang_name = LANGUAGE_NAMES[normalize_language(language)]
    existing = "\n".join(f"- {term}: {definition}" for term, definition in glossary.items()) or "(none yet)"
    instructions = (
        f"Glossary so far:\n{existing}\n\n"
        "List any invented names, places, factions, philosophies, or concepts "
        "on this page that a first-time reader would need defined or reminded "
        "of — especially ones central to understanding later parts of the "
        "book. Skip real-world common words, and skip anything already "
        "correctly covered by the glossary above unless its meaning has "
        "meaningfully deepened or changed. Keep definitions purely factual — "
        "don't reveal a character's fate, loyalty, or any future plot "
        "development that hasn't happened on this page yet.\n\n"
        f"Reply in {lang_name}, one entry per line, exactly in this format:\n"
        "Term: one-sentence definition\n\n"
        "Reply with just the word NONE if there's nothing to add or update."
    )

    try:
        if page.get("image_data"):
            content = [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": page["media_type"], "data": page["image_data"]},
                },
                {"type": "text", "text": instructions},
            ]
        else:
            content = f"Page text:\n{page['text'][:2000]}\n\n{instructions}"

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": content}],
        )
        reply = msg.content[0].text.strip()
        if reply.upper() == "NONE":
            return glossary

        updated = dict(glossary)
        for line in reply.splitlines():
            if ":" not in line:
                continue
            term, _, definition = line.partition(":")
            term = term.strip().lstrip("-•").strip()
            definition = definition.strip()
            if term and definition:
                updated[term] = definition
        return updated
    except Exception:
        return glossary


# ── Prompts ───────────────────────────────────────────────────────────────────

PROMPTS = {
    "short": """\
You're a friend who already read this book, walking someone who doesn't read much through it page by page.

You're given context about the book, a running story summary (if available), a glossary of invented names/terms/concepts defined so far (if available), the previous page (if available), and the current page.

Important: pages are physical PDF pages, so they often cut off mid-sentence. That's normal. If the page ends mid-thought, don't just stop — briefly say what the scene or moment was building toward, even if it wasn't finished on this page.

Write 3-4 short casual paragraphs. No headers. No bullet points. No bold labels. No section names. No "Bridge:" or "Bottom line:" prefixes. Just talk to them like you're sending a voice note.

Use the book overview and story summary to open with one grounding sentence — where are we in the story right now? Then explain what's happening on this page in plain, simple language, slipping in character reminders naturally as they come up ("Paul, the main character" or "Jessica, his mom"). Let your last sentence land on what this page was building toward — even if it got cut off.

Under 150 words. Casual and warm.""",

    "medium": """\
You're a friend who already read this book, walking someone who doesn't read much through it page by page. They struggle to connect ideas — make every link feel natural and obvious.

You're given context about the book, a running story summary (if available), a glossary of invented names/terms/concepts defined so far (if available), the previous page (if available), and the current page.

Important: pages are physical PDF pages, so they often cut off mid-sentence. That's normal. If the page ends mid-thought, don't just stop there — briefly tell them what the scene was heading toward and that it continues on the next page, so nothing feels like it's just dying out.

Write flowing paragraphs. No headers. No bullet points. No bold section labels. No "Bridge:" or "Watch for:" prefixes. No numbered sections. Just talk.

Use the book overview and summary to open with a sentence or two grounding them in the bigger story. Then walk through this page idea by idea, in order, naturally bridging from the previous page as you go. Slip character and concept reminders in as they come up. Let everything connect without announcing the connections. End on one plain sentence about where the page was heading — even if it cut off mid-scene.

Sound like a friend, not a teacher filling out a form.""",

    "long": """\
You're a patient friend who already read this book carefully, walking someone through it page by page. They struggle to connect ideas between sentences, between pages, and across the whole book — leave nothing implicit, but make it feel natural.

You're given context about the book, a running story summary (if available), a glossary of invented names/terms/concepts defined so far (if available), the previous page (if available), and the current page.

Important: pages are physical PDF pages, so they often cut off mid-sentence or mid-scene. That's normal — don't let the explanation just trail off when this happens. If the page ends mid-thought, explicitly tell them: what was the scene building toward, what's the tension that's about to pay off, and that it continues on the next page. Make the cut-off feel like a cliffhanger, not an oversight.

Write in flowing paragraphs. No headers. No bullet points. No bold labels. No "Bridge from last page:" or "Big picture:" or any section prefixes. Just talk to them like you're sitting right next to them.

Open by grounding them in the bigger story using the book overview and summary — what's been building, and where does this page fit? Let the flow from the previous page come through naturally. Walk through every idea on this page in order: say what's happening plainly, explain why the author put it here, remind them who characters and concepts are as they come up, connect each idea to the one before it. Tie this page to the overall arc somewhere in there. End by telling them exactly where the page left off and what's about to happen next — make them want to turn the page.

Warm, thorough, and conversational throughout.""",

    "recap": """\
You're a friend who already read this book, walking someone who doesn't read much through it page by page.

You're given context about the book, a running story summary (if available), a glossary of invented names/terms/concepts defined so far (if available), the previous page (if available), and the current page.

Important: pages are physical PDF pages, so they often cut off mid-sentence. That's normal. If the page ends mid-thought, don't just stop — briefly say what the scene or moment was building toward, even if it wasn't finished on this page.

If this page branches away from the main story — a flashback, a memory, a story a character is telling, a different time or place, a side conversation — briefly flag it when it starts ("quick detour — this is...") and clearly flag when we're back on the main thread ("okay, back to [whoever]..."). Don't skip or shorten what happens in the detour — explain it with the same care as the main story, just make it clear it's a branch so it doesn't get confused for the main plot.

Write 3-4 short casual paragraphs. No headers. No bullet points. No bold labels. No section names. No "Bridge:" or "Bottom line:" prefixes. Just talk to them like you're sending a voice note.

Use the book overview and story summary to open with one grounding sentence — where are we in the story right now? Then explain what's happening on this page in plain, simple language, slipping in character reminders naturally as they come up ("Paul, the main character" or "Jessica, his mom"). Let your last sentence land on what this page was building toward — even if it got cut off.

Under 150 words. Casual and warm.""",

    "vivid": """\
You're a friend who already read this book, walking someone who doesn't read much through it page by page.

You're given context about the book, a running story summary (if available), a glossary of invented names/terms/concepts defined so far (if available), the previous page (if available), and the current page.

Important: pages are physical PDF pages, so they often cut off mid-sentence. That's normal. If the page ends mid-thought, don't just stop — briefly say what the scene or moment was building toward, even if it wasn't finished on this page.

If this page branches away from the main story — a flashback, a memory, a story a character is telling, a different time or place, a side conversation — briefly flag it when it starts ("quick detour — this is...") and clearly flag when we're back on the main thread ("okay, back to [whoever]..."). Don't skip or shorten what happens in the detour — explain it with the same care as the main story, just make it clear it's a branch so it doesn't get confused for the main plot.

Some books alternate scenes between different characters' perspectives — one character's storyline, then a different character's, then back again — sometimes more than once on the same page. Whenever that shift happens, say so plainly when it happens ("switching over to [character] now" or "back to [character]'s side of things") so it's always clear whose eyes we're currently seeing events through, the same way you'd flag a flashback.

Don't just report what happened — put them in it. When something happens, say what it looked like, sounded like, or felt like in the character's body, and name what they're actually feeling in the moment (nervous, relieved, furious, whatever it is) — in plain words, not fancy ones. A short, simple beat like "his stomach dropped" or "the room went quiet" does more than a flowery one. But the vivid detail is never allowed to crowd out the plain connective explanation — why this is happening, how it connects to what came before, what it means for where the story's headed. Both have to be there; don't trade one for the other.

Write a handful of short casual paragraphs. No headers. No bullet points. No bold labels. No section names. No "Bridge:" or "Bottom line:" prefixes. Just talk to them like you're sending a voice note.

Use the book overview and story summary to open with one grounding sentence — where are we in the story right now? Then walk through what's happening on this page, putting them in the scene as it unfolds, slipping in character reminders naturally as they come up ("Paul, the main character" or "Jessica, his mom"), and connecting each idea to the one before it so nothing feels like it came out of nowhere. Let your last sentence land on what this page was building toward — even if it got cut off.

Under 250 words. Casual, warm, vivid, and easy to follow.""",
}

CHECKPOINT_EVERY_N_PAGES = 3

# Each mode with a fixed word cap, matching the "Under N words" line written
# into its own PROMPTS text — Vivid's is higher than Short/Recap's on purpose,
# so its added sensory/emotional detail gets its own room instead of crowding
# out the plain connective explanation out of the same tight budget.
WORDS_PER_PAGE = {"short": 150, "recap": 150, "vivid": 250}

# Modes that get the every-Nth-page catch-up recap.
CHECKPOINT_MODES = ("recap", "vivid")


def build_system_prompt(mode: str, num_pages: int, lang_name: str, checkpoint: bool = False) -> str:
    """The base PROMPTS text is written for exactly one page. For a single
    page (the normal case) it's used unchanged. When several pages are
    being explained in one go, append instructions telling Claude to give
    each page its own full-depth section instead of blending or compressing
    them to fit — otherwise a multi-page request would cram several pages'
    worth of story into the same short word count meant for just one."""
    base = PROMPTS[mode]
    if num_pages > 1:
        multi_page_note = (
            f"\n\nYou're covering {num_pages} pages in this single response, not just one. "
            f"Treat it as {num_pages} separate page-explanations back to back — don't blend, "
            "compress, or summarize across pages to save space. Before each page's section, put "
            'a short heading on its own line like "Page 12" (using that page\'s own page number), '
            "then give that page the exact same style and level of detail described above, as if "
            "it were the only page you were explaining."
        )
        if mode in WORDS_PER_PAGE:
            words_per_page = WORDS_PER_PAGE[mode]
            total_words = words_per_page * num_pages
            multi_page_note += (
                f" Each page's section should still be under {words_per_page} words on "
                f"its own, same as if it were alone — so roughly {total_words} words total, not one "
                "squeezed-down summary."
            )
        base += multi_page_note
    if mode in CHECKPOINT_MODES and checkpoint:
        base += (
            f"\n\nThis is a checkpoint page — every {CHECKPOINT_EVERY_N_PAGES} pages, the reader gets "
            "a quick catch-up so a long book doesn't lose them. Before walking through this page, start "
            "with a short 2-3 sentence recap of what's happened over the last few pages, in the same "
            "plain casual language as everything else. Keep it clearly separate from the walkthrough "
            "that follows — a line break is enough, no header or label — then continue into the normal "
            "page walkthrough exactly as described above."
        )
    return base + f"\n\nWrite your entire response in {lang_name}, regardless of what language the page itself is written in."


# ── Routes ────────────────────────────────────────────────────────────────────

async def build_content_from_bytes(raw: bytes, suffix: str, title: str, language: str) -> tuple[dict, str]:
    """Shared by /upload (a fresh file) and /library/open (reopening a
    previously saved one) — turns raw file bytes into the same response
    shape the frontend expects either way. Returns (response, content_hash)."""
    if suffix == ".pdf":
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            pages = await asyncio.to_thread(extract_pdf_pages, tmp_path)
        except Exception as e:
            raise HTTPException(500, f"Could not parse PDF: {e}")
        finally:
            os.unlink(tmp_path)

    elif suffix in {".cbr", ".cbz"}:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            pages = await asyncio.to_thread(extract_comic_pages, tmp_path)
        except Exception as e:
            raise HTTPException(500, f"Could not parse comic file: {e}")
        finally:
            os.unlink(tmp_path)

    else:
        text = raw.decode("utf-8", errors="ignore")
        pages = await asyncio.to_thread(extract_text_pages, text)
    if not pages:
        raise HTTPException(400, "No readable content found.")

    is_comic = suffix in {".cbr", ".cbz"}

    try:
        overview = await asyncio.to_thread(generate_book_overview, pages, language)
    except Exception:
        overview = ""

    if is_comic:
        skip_indices = set()
    else:
        try:
            skip_indices = await asyncio.to_thread(classify_front_matter, pages)
        except Exception:
            skip_indices = set()
    for p in pages:
        p["skip"] = p["index"] in skip_indices

    content_id = str(uuid.uuid4())
    content_hash = hashlib.sha256(raw).hexdigest()
    saved = await asyncio.to_thread(get_saved_progress, content_hash)
    content_store[content_id] = {
        "title": title,
        "pages": pages,
        "overview": overview,
        "pdf_bytes": raw if suffix == ".pdf" else None,
        "raw_text": raw.decode("utf-8", errors="ignore") if suffix in {".txt", ".md"} else None,
        "original_url": None,
        "content_hash": content_hash,
    }

    has_page_images = is_comic or suffix == ".pdf"
    response = {
        "content_id": content_id,
        "content_hash": content_hash,
        "title": title,
        "overview": overview,
        "total_pages": len(pages),
        "last_page_num": pages[-1]["page_num"],
        "is_comic": is_comic,
        "has_page_images": has_page_images,
        "document_url": f"/document?content_id={content_id}",
        "resume_index": saved["page_index"] if saved else None,
        "resume_glossary": saved["glossary"] if saved else {},
        "pages": [
            {"index": p["index"], "title": p["title"], "page_num": p["page_num"], "skip": p["skip"]}
            for p in pages
        ],
    }
    return response, content_hash


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), language: str = Form("english")):
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in {".pdf", ".txt", ".md", ".cbr", ".cbz"}:
        raise HTTPException(400, f"Unsupported type '{suffix}'. Upload PDF, TXT, MD, CBR, or CBZ.")

    raw = await file.read()
    response, content_hash = await build_content_from_bytes(raw, suffix, file.filename, language)
    await asyncio.to_thread(save_file_to_library, content_hash, file.filename, file.filename, suffix, raw, None)
    return response


class UrlUploadRequest(BaseModel):
    url: str
    language: str = "english"


async def build_content_from_url(url: str, language: str) -> tuple[dict, str, bytes | None]:
    """Shared by /upload-url and /library/open — fetches and processes a web
    link into the same response shape the frontend expects either way.
    Returns (response, content_hash, pdf_bytes) — pdf_bytes is only non-None
    for a direct PDF link, since a plain article is re-fetched from the URL
    rather than stored."""
    try:
        content = await asyncio.to_thread(fetch_url_content, url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not fetch that link: {e}")

    title, pages = content["title"], content["pages"]
    if not pages:
        raise HTTPException(400, "No readable content found at that link.")

    try:
        overview = await asyncio.to_thread(generate_book_overview, pages, language)
    except Exception:
        overview = ""

    try:
        skip_indices = await asyncio.to_thread(classify_front_matter, pages)
    except Exception:
        skip_indices = set()
    for p in pages:
        p["skip"] = p["index"] in skip_indices

    content_id = str(uuid.uuid4())
    is_pdf_link = content["pdf_bytes"] is not None
    content_hash = hashlib.sha256(
        content["pdf_bytes"] if is_pdf_link else url.encode()
    ).hexdigest()
    saved = await asyncio.to_thread(get_saved_progress, content_hash)
    content_store[content_id] = {
        "title": title,
        "pages": pages,
        "overview": overview,
        "pdf_bytes": content["pdf_bytes"],
        "raw_text": None,
        "original_url": url,
        "content_hash": content_hash,
    }

    response = {
        "content_id": content_id,
        "content_hash": content_hash,
        "title": title,
        "overview": overview,
        "total_pages": len(pages),
        "last_page_num": pages[-1]["page_num"],
        "is_comic": False,
        "has_page_images": content["has_page_images"],
        "document_url": f"/document?content_id={content_id}" if is_pdf_link else url,
        "resume_index": saved["page_index"] if saved else None,
        "resume_glossary": saved["glossary"] if saved else {},
        "pages": [
            {"index": p["index"], "title": p["title"], "page_num": p["page_num"], "skip": p["skip"]}
            for p in pages
        ],
    }
    return response, content_hash, content["pdf_bytes"]


@app.post("/upload-url")
async def upload_url(req: UrlUploadRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Please provide a link.")

    try:
        await asyncio.to_thread(validate_public_url, url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    response, content_hash, pdf_bytes = await build_content_from_url(url, req.language)
    await asyncio.to_thread(
        save_file_to_library, content_hash, response["title"], None, ".pdf" if pdf_bytes else None, pdf_bytes, url
    )
    return response


@app.get("/library")
async def get_library():
    """Every book saved from a previous upload, so the frontend can list
    them without needing the raw file contents."""
    books = await asyncio.to_thread(list_library_books)
    return {"books": books}


class LibraryOpenRequest(BaseModel):
    content_hash: str
    language: str = "english"


@app.post("/library/open")
async def open_from_library(req: LibraryOpenRequest):
    """Reopen a previously saved book without re-uploading it — pulls the
    saved file (or re-fetches the saved URL) and runs it through the exact
    same pipeline a fresh upload would."""
    record = await asyncio.to_thread(load_library_file, req.content_hash)
    if not record:
        raise HTTPException(404, "That book isn't in your library.")

    if record["file_bytes"] is not None:
        title = record["filename"] or record["title"] or "Untitled"
        response, _ = await build_content_from_bytes(
            record["file_bytes"], record["suffix"] or ".txt", title, req.language
        )
        return response
    if record["original_url"]:
        response, _, _ = await build_content_from_url(record["original_url"], req.language)
        return response
    raise HTTPException(404, "Nothing saved for this book — try uploading it again.")


class FavoriteRequest(BaseModel):
    content_hash: str


@app.post("/library/favorite")
async def favorite_book(req: FavoriteRequest):
    ok = await asyncio.to_thread(set_favorite_book, req.content_hash)
    return {"saved": ok}


@app.get("/library/favorite")
async def get_favorite():
    content_hash = await asyncio.to_thread(get_favorite_book)
    return {"content_hash": content_hash}


class ExplainRequest(BaseModel):
    content_id: str
    page_indices: list[int]
    mode: str
    summary: str = ""  # rolling "story so far" built up as the user reads
    glossary: dict[str, str] = {}  # invented names/terms/concepts defined so far
    language: str = "english"


class SummarizeRequest(BaseModel):
    content_id: str
    page_indices: list[int]
    current_summary: str = ""
    glossary: dict[str, str] = {}
    language: str = "english"


@app.post("/explain")
async def explain(req: ExplainRequest, request: Request):
    if req.content_id not in content_store:
        raise HTTPException(404, "Content not found. Please re-upload your file.")

    store = content_store[req.content_id]
    pages = store["pages"]
    overview = store.get("overview", "")

    if not req.page_indices:
        raise HTTPException(400, "No pages given.")
    for idx in req.page_indices:
        if idx < 0 or idx >= len(pages):
            raise HTTPException(400, "Invalid page index.")

    group = [pages[idx] for idx in req.page_indices]
    first_page, first_index = group[0], req.page_indices[0]

    is_comic = bool(first_page.get("image_data"))
    if not is_comic and sum(len(p["text"].strip()) for p in group) < 80:
        raise HTTPException(400, "This page has too little content to explain.")

    mode = req.mode if req.mode in PROMPTS else "medium"
    lang_name = LANGUAGE_NAMES[normalize_language(req.language)]
    # Recap mode gets a quick catch-up every few pages so a long book doesn't
    # lose the reader — based on this group's last page, so it fires on the
    # same physical pages regardless of how many are bundled per request.
    last_index = req.page_indices[-1]
    checkpoint = (last_index + 1) % CHECKPOINT_EVERY_N_PAGES == 0
    system_prompt = build_system_prompt(mode, len(group), lang_name, checkpoint)

    # Build context: book overview → rolling summary → glossary → previous page → current page(s)
    parts = []
    if overview:
        parts.append(f'About this book:\n"""\n{overview}\n"""')
    if req.summary.strip():
        parts.append(f'Story so far:\n"""\n{req.summary.strip()}\n"""')
    if req.glossary:
        glossary_lines = "\n".join(f"- {term}: {definition}" for term, definition in req.glossary.items())
        parts.append(
            'Glossary of invented names, terms, and concepts defined so far:\n"""\n'
            f'{glossary_lines}\n"""\n'
            "If any of these resurface on the current page, briefly remind the reader "
            "what they mean in passing — don't assume they remember a term from way "
            "earlier, the way this glossary does."
        )
    if first_index > 0:
        prev = pages[first_index - 1]
        parts.append(f'Previous page ({prev["title"]}):\n"""\n{prev["text"][:2500]}\n"""')
    if len(group) == 1:
        parts.append(f'Current page ({first_page["title"]}):\n"""\n{first_page["text"]}\n"""')
        parts.append("Walk me through this page.")
    else:
        page_blocks = "\n\n".join(f'Page {p["page_num"]} ({p["title"]}):\n"""\n{p["text"]}\n"""' for p in group)
        parts.append(f"Current pages:\n{page_blocks}")
        parts.append("Walk me through these pages, one at a time, in order.")

    user_msg = "\n\n".join(parts)

    model = "claude-sonnet-4-6" if mode == "long" else "claude-haiku-4-5-20251001"

    # How much Claude is *allowed* to write back is a separate, hard ceiling
    # from any word-count instruction in the prompt — hitting it cuts the
    # reply off mid-sentence with no warning. Scale it up with how many pages
    # are being covered in one go, capped well inside each model's own real
    # output limit so this never turns into a request error instead.
    if model == "claude-sonnet-4-6":
        max_output_tokens = min(6000 * len(group), 32000)
    else:
        max_output_tokens = min(max(4000, 2500 * len(group)), 8000)

    # Build message content — comic pages use vision, text pages use plain text
    if is_comic:
        # Include previous page image + each current page's image, in order
        vision_content = []
        if first_index > 0:
            prev = pages[first_index - 1]
            if prev.get("image_data"):
                vision_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": prev["media_type"], "data": prev["image_data"]},
                })
                vision_content.append({"type": "text", "text": f"(Previous page — {prev['title']})"})
        for p in group:
            if p.get("image_data"):
                vision_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": p["media_type"], "data": p["image_data"]},
                })
                if len(group) > 1:
                    vision_content.append({"type": "text", "text": f"(Page {p['page_num']} — {p['title']})"})
        vision_content.append({"type": "text", "text": user_msg})
        message_content = vision_content
    else:
        message_content = user_msg

    async def generate():
        try:
            disconnected = False
            async with async_client.messages.stream(
                model=model,
                max_tokens=max_output_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": message_content}],
                timeout=120.0,
            ) as stream:
                async for text in stream.text_stream:
                    # Stop burning Claude API time and server resources the
                    # moment the client gives up (navigated away, closed the
                    # tab) instead of running the full generation regardless —
                    # this is what was piling up under rapid navigation and
                    # starving out new requests on a resource-limited instance.
                    if await request.is_disconnected():
                        disconnected = True
                        break
                    yield f"data: {json.dumps({'text': text})}\n\n"

                # Even with the scaled-up ceiling above, say so plainly if a
                # reply still ran out of room, instead of silently ending
                # mid-sentence and leaving the reader to wonder what happened.
                if not disconnected:
                    final = await stream.get_final_message()
                    if final.stop_reason == "max_tokens":
                        note = (
                            "\n\n_(This ran longer than expected and got cut off here — "
                            "try fewer pages at a time, or tap re-explain to try again.)_"
                        )
                        yield f"data: {json.dumps({'text': note})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/summarize")
async def summarize(req: SummarizeRequest):
    """Update the rolling story summary after a page (or several pages, read
    together as one group) is read."""
    if req.content_id not in content_store:
        raise HTTPException(404, "Content not found.")

    pages = content_store[req.content_id]["pages"]

    if not req.page_indices:
        raise HTTPException(400, "No pages given.")
    for idx in req.page_indices:
        if idx < 0 or idx >= len(pages):
            raise HTTPException(400, "Invalid page index.")

    group = [pages[idx] for idx in req.page_indices]
    last_index = req.page_indices[-1]
    current = req.current_summary.strip()
    lang_name = LANGUAGE_NAMES[normalize_language(req.language)]

    is_comic = bool(group[0].get("image_data"))
    summary_prefix = f"Current summary:\n{current}\n\n" if current else ""
    coverage = "this page" if len(group) == 1 else f"these {len(group)} pages"

    if is_comic:
        content = []
        for p in group:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": p["media_type"], "data": p["image_data"]},
            })
        content.append({
            "type": "text",
            "text": (
                f"{summary_prefix}This is comic book page {group[0]['page_num']}."
                if len(group) == 1 else
                f"{summary_prefix}These are comic book pages {group[0]['page_num']}–{group[-1]['page_num']}, in order."
            ) + (
                f" Write a 2–4 sentence plain, casual {lang_name} summary covering everything up to and "
                f"including {coverage}. Keep it casual, focused on the main story thread, and written like "
                "a quick catch-up for a friend. Return only the summary text, nothing else."
            ),
        })
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": content}],
        )
    else:
        if len(group) == 1:
            pages_blob = f"New page ({group[0]['title']}):\n{group[0]['text'][:2000]}\n\n"
        else:
            pages_blob = "\n\n".join(
                f'Page {p["page_num"]} ({p["title"]}):\n{p["text"][:2000]}' for p in group
            ) + "\n\n"
        prompt = (
            f"{summary_prefix}{pages_blob}"
            f"Write a 2–4 sentence plain, casual {lang_name} summary covering everything up to and including {coverage}. "
            f"Keep it casual, focused on the main story thread, and written like a quick catch-up for a friend. "
            f"Return only the summary text, nothing else."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )

    # Peek at the next page so the UI can show a "coming up" teaser
    next_teaser = ""
    if last_index + 1 < len(pages):
        next_text = pages[last_index + 1]["text"]
        next_teaser = " ".join(next_text.split()[:30])

    # Run the glossary update once per page, in order, feeding each result
    # into the next — the end result is identical to reading these pages
    # one at a time, just gathered into a single response.
    glossary = req.glossary
    for p in group:
        glossary = await asyncio.to_thread(update_glossary, glossary, p, req.language)

    return {"summary": msg.content[0].text.strip(), "next_teaser": next_teaser, "glossary": glossary}


DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs "Rachel" (English)
# Set ELEVENLABS_VOICE_ID_ES in your environment to a voice from your own
# ElevenLabs Voice Library for a proper Spanish/Mexican-accented voice —
# falls back to the English default (still intelligible, multilingual model)
# if not configured.
SPANISH_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID_ES", DEFAULT_VOICE_ID)


class TTSRequest(BaseModel):
    text: str = ""           # explanation text (sent from frontend)
    content_id: str = ""     # for reading raw page text
    page_index: int = -1     # for reading raw page text
    language: str = "english"
    voice_id: str = ""       # explicit override; empty = pick by language


@app.post("/tts")
async def text_to_speech(req: TTSRequest):
    import requests as req_lib
    from fastapi.responses import Response

    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise HTTPException(500, "ELEVENLABS_API_KEY is not set. Add it to your environment variables.")

    voice_id = req.voice_id or (
        SPANISH_VOICE_ID if normalize_language(req.language) == "spanish" else DEFAULT_VOICE_ID
    )

    # Get the text to speak
    if req.text.strip():
        text = req.text.strip()[:4500]
    elif req.content_id and req.page_index >= 0:
        store = content_store.get(req.content_id)
        if not store:
            raise HTTPException(404, "Content not found.")
        if req.page_index >= len(store["pages"]):
            raise HTTPException(400, "Invalid page index.")
        text = store["pages"][req.page_index]["text"][:4500]
    else:
        raise HTTPException(400, "Provide either text or content_id + page_index.")

    def call_elevenlabs():
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        r = req_lib.post(
            url,
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=30,
        )
        if not r.ok:
            raise Exception(f"ElevenLabs {r.status_code}: {r.text}")
        r.raise_for_status()
        return r.content

    try:
        audio_bytes = await asyncio.to_thread(call_elevenlabs)
    except Exception as e:
        raise HTTPException(502, f"ElevenLabs error: {e}")

    return Response(content=audio_bytes, media_type="audio/mpeg")


@app.get("/document")
async def get_document(content_id: str):
    import re
    from fastapi.responses import Response, PlainTextResponse, HTMLResponse

    store = content_store.get(content_id)
    if not store:
        raise HTTPException(404, "Content not found.")

    safe_title = re.sub(r'[^\w\-. ]', '_', store.get("title") or "document") or "document"

    if store.get("pdf_bytes"):
        filename = safe_title if safe_title.lower().endswith(".pdf") else f"{safe_title}.pdf"
        return Response(
            content=store["pdf_bytes"],
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    if store.get("raw_text") is not None:
        return PlainTextResponse(store["raw_text"])

    pages = store["pages"]
    if pages and pages[0].get("image_data"):
        imgs = "".join(
            f'<img src="/page-image?content_id={content_id}&page_index={p["index"]}" '
            f'style="display:block;width:100%;max-width:900px;margin:0 auto;" />'
            for p in pages
        )
        html = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            f"<title>{safe_title}</title>"
            "<style>body{background:#111;margin:0;padding:0;}</style></head>"
            f"<body>{imgs}</body></html>"
        )
        return HTMLResponse(html)

    raise HTTPException(404, "No full document available for this content.")


@app.get("/page-image")
async def get_page_image(content_id: str, page_index: int):
    from fastapi.responses import Response
    store = content_store.get(content_id)
    if not store:
        raise HTTPException(404, "Content not found.")
    pages = store["pages"]
    if page_index < 0 or page_index >= len(pages):
        raise HTTPException(400, "Invalid page index.")
    page = pages[page_index]

    if page.get("image_data"):
        import base64
        image_bytes = base64.b64decode(page["image_data"])
        return Response(content=image_bytes, media_type=page.get("media_type", "image/jpeg"))

    pdf_bytes = store.get("pdf_bytes")
    if pdf_bytes:
        # Rendering a PDF page is CPU-heavy; cache it per content so flipping
        # back and forth between pages (or opening the same page twice)
        # doesn't re-render it from scratch every single time.
        image_cache = store.setdefault("rendered_image_cache", {})
        image_bytes = image_cache.get(page_index)
        if image_bytes is None:
            try:
                image_bytes = await asyncio.to_thread(render_pdf_page_image, pdf_bytes, page["page_num"])
            except Exception as e:
                raise HTTPException(500, f"Could not render page image: {e}")
            image_cache[page_index] = image_bytes
        return Response(content=image_bytes, media_type="image/jpeg")

    raise HTTPException(404, "This page has no image.")


@app.get("/page-text")
async def get_page_text(content_id: str, page_index: int):
    store = content_store.get(content_id)
    if not store:
        raise HTTPException(404, "Content not found.")
    pages = store["pages"]
    if page_index < 0 or page_index >= len(pages):
        raise HTTPException(400, "Invalid page index.")
    return {"text": pages[page_index]["text"]}


class ProgressRequest(BaseModel):
    content_id: str
    page_index: int
    glossary: dict[str, str] = {}


@app.post("/progress")
async def save_reading_progress(req: ProgressRequest):
    """Remember the current page and glossary for this book so it can be
    resumed on any device. Best-effort — silently does nothing if there's no
    database configured or it's briefly unreachable, since this is a
    nice-to-have, not something that should ever block reading."""
    store = content_store.get(req.content_id)
    if not store or not store.get("content_hash"):
        return {"saved": False}
    saved = await asyncio.to_thread(
        save_progress, store["content_hash"], store.get("title") or "", req.page_index, req.glossary
    )
    return {"saved": saved}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug-key")
async def debug_key():
    key = os.getenv("ELEVENLABS_API_KEY", "")
    if not key:
        return {"key_set": False, "preview": ""}
    return {"key_set": True, "preview": key[:8] + "..." + key[-4:]}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
