import os
import json
import uuid
import tempfile
import asyncio

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


# ── Prompts ───────────────────────────────────────────────────────────────────

PROMPTS = {
    "short": """\
You're a friend who already read this book, walking someone who doesn't read much through it page by page.

You're given context about the book, a running story summary (if available), the previous page (if available), and the current page.

Important: pages are physical PDF pages, so they often cut off mid-sentence. That's normal. If the page ends mid-thought, don't just stop — briefly say what the scene or moment was building toward, even if it wasn't finished on this page.

Write 3-4 short casual paragraphs. No headers. No bullet points. No bold labels. No section names. No "Bridge:" or "Bottom line:" prefixes. Just talk to them like you're sending a voice note.

Use the book overview and story summary to open with one grounding sentence — where are we in the story right now? Then explain what's happening on this page in plain, simple language, slipping in character reminders naturally as they come up ("Paul, the main character" or "Jessica, his mom"). Let your last sentence land on what this page was building toward — even if it got cut off.

Under 150 words. Casual and warm.""",

    "medium": """\
You're a friend who already read this book, walking someone who doesn't read much through it page by page. They struggle to connect ideas — make every link feel natural and obvious.

You're given context about the book, a running story summary (if available), the previous page (if available), and the current page.

Important: pages are physical PDF pages, so they often cut off mid-sentence. That's normal. If the page ends mid-thought, don't just stop there — briefly tell them what the scene was heading toward and that it continues on the next page, so nothing feels like it's just dying out.

Write flowing paragraphs. No headers. No bullet points. No bold section labels. No "Bridge:" or "Watch for:" prefixes. No numbered sections. Just talk.

Use the book overview and summary to open with a sentence or two grounding them in the bigger story. Then walk through this page idea by idea, in order, naturally bridging from the previous page as you go. Slip character and concept reminders in as they come up. Let everything connect without announcing the connections. End on one plain sentence about where the page was heading — even if it cut off mid-scene.

Sound like a friend, not a teacher filling out a form.""",

    "long": """\
You're a patient friend who already read this book carefully, walking someone through it page by page. They struggle to connect ideas between sentences, between pages, and across the whole book — leave nothing implicit, but make it feel natural.

You're given context about the book, a running story summary (if available), the previous page (if available), and the current page.

Important: pages are physical PDF pages, so they often cut off mid-sentence or mid-scene. That's normal — don't let the explanation just trail off when this happens. If the page ends mid-thought, explicitly tell them: what was the scene building toward, what's the tension that's about to pay off, and that it continues on the next page. Make the cut-off feel like a cliffhanger, not an oversight.

Write in flowing paragraphs. No headers. No bullet points. No bold labels. No "Bridge from last page:" or "Big picture:" or any section prefixes. Just talk to them like you're sitting right next to them.

Open by grounding them in the bigger story using the book overview and summary — what's been building, and where does this page fit? Let the flow from the previous page come through naturally. Walk through every idea on this page in order: say what's happening plainly, explain why the author put it here, remind them who characters and concepts are as they come up, connect each idea to the one before it. Tie this page to the overall arc somewhere in there. End by telling them exactly where the page left off and what's about to happen next — make them want to turn the page.

Warm, thorough, and conversational throughout.""",
}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), language: str = Form("english")):
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in {".pdf", ".txt", ".md", ".cbr", ".cbz"}:
        raise HTTPException(400, f"Unsupported type '{suffix}'. Upload PDF, TXT, MD, CBR, or CBZ.")

    raw = await file.read()

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
    content_store[content_id] = {
        "title": file.filename,
        "pages": pages,
        "overview": overview,
        "pdf_bytes": raw if suffix == ".pdf" else None,
        "raw_text": raw.decode("utf-8", errors="ignore") if suffix in {".txt", ".md"} else None,
        "original_url": None,
    }

    has_page_images = is_comic or suffix == ".pdf"
    return {
        "content_id": content_id,
        "title": file.filename,
        "overview": overview,
        "total_pages": len(pages),
        "last_page_num": pages[-1]["page_num"],
        "is_comic": is_comic,
        "has_page_images": has_page_images,
        "document_url": f"/document?content_id={content_id}",
        "pages": [
            {"index": p["index"], "title": p["title"], "page_num": p["page_num"], "skip": p["skip"]}
            for p in pages
        ],
    }


class UrlUploadRequest(BaseModel):
    url: str
    language: str = "english"


@app.post("/upload-url")
async def upload_url(req: UrlUploadRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Please provide a link.")

    try:
        await asyncio.to_thread(validate_public_url, url)
    except ValueError as e:
        raise HTTPException(400, str(e))

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
        overview = await asyncio.to_thread(generate_book_overview, pages, req.language)
    except Exception:
        overview = ""

    try:
        skip_indices = await asyncio.to_thread(classify_front_matter, pages)
    except Exception:
        skip_indices = set()
    for p in pages:
        p["skip"] = p["index"] in skip_indices

    content_id = str(uuid.uuid4())
    content_store[content_id] = {
        "title": title,
        "pages": pages,
        "overview": overview,
        "pdf_bytes": content["pdf_bytes"],
        "raw_text": None,
        "original_url": url,
    }

    is_pdf_link = content["pdf_bytes"] is not None
    return {
        "content_id": content_id,
        "title": title,
        "overview": overview,
        "total_pages": len(pages),
        "last_page_num": pages[-1]["page_num"],
        "is_comic": False,
        "has_page_images": content["has_page_images"],
        "document_url": f"/document?content_id={content_id}" if is_pdf_link else url,
        "pages": [
            {"index": p["index"], "title": p["title"], "page_num": p["page_num"], "skip": p["skip"]}
            for p in pages
        ],
    }


class ExplainRequest(BaseModel):
    content_id: str
    page_index: int
    mode: str
    summary: str = ""  # rolling "story so far" built up as the user reads
    language: str = "english"


class SummarizeRequest(BaseModel):
    content_id: str
    page_index: int
    current_summary: str = ""
    language: str = "english"


@app.post("/explain")
async def explain(req: ExplainRequest, request: Request):
    if req.content_id not in content_store:
        raise HTTPException(404, "Content not found. Please re-upload your file.")

    store = content_store[req.content_id]
    pages = store["pages"]
    overview = store.get("overview", "")

    if req.page_index < 0 or req.page_index >= len(pages):
        raise HTTPException(400, "Invalid page index.")

    page = pages[req.page_index]

    is_comic_page = bool(page.get("image_data"))
    if not is_comic_page and len(page["text"].strip()) < 80:
        raise HTTPException(400, "This page has too little content to explain.")

    mode = req.mode if req.mode in PROMPTS else "medium"
    lang_name = LANGUAGE_NAMES[normalize_language(req.language)]
    system_prompt = (
        PROMPTS[mode]
        + f"\n\nWrite your entire response in {lang_name}, regardless of what language the page itself is written in."
    )

    # Build context: book overview → rolling summary → previous page → current page
    parts = []
    if overview:
        parts.append(f'About this book:\n"""\n{overview}\n"""')
    if req.summary.strip():
        parts.append(f'Story so far:\n"""\n{req.summary.strip()}\n"""')
    if req.page_index > 0:
        prev = pages[req.page_index - 1]
        parts.append(f'Previous page ({prev["title"]}):\n"""\n{prev["text"][:2500]}\n"""')
    parts.append(f'Current page ({page["title"]}):\n"""\n{page["text"]}\n"""')
    parts.append("Walk me through this page.")

    user_msg = "\n\n".join(parts)

    model = "claude-sonnet-4-6" if mode == "long" else "claude-haiku-4-5-20251001"

    # Build message content — comic pages use vision, text pages use plain text
    is_comic = bool(page.get("image_data"))
    if is_comic:
        # Include previous page image + current page image if available
        vision_content = []
        if req.page_index > 0:
            prev = pages[req.page_index - 1]
            if prev.get("image_data"):
                vision_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": prev["media_type"], "data": prev["image_data"]},
                })
                vision_content.append({"type": "text", "text": f"(Previous page — {prev['title']})"})
        vision_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": page["media_type"], "data": page["image_data"]},
        })
        vision_content.append({"type": "text", "text": user_msg})
        message_content = vision_content
    else:
        message_content = user_msg

    async def generate():
        try:
            async with async_client.messages.stream(
                model=model,
                max_tokens=4000,
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
                        break
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/summarize")
async def summarize(req: SummarizeRequest):
    """Update the rolling story summary after a page is read."""
    if req.content_id not in content_store:
        raise HTTPException(404, "Content not found.")

    pages = content_store[req.content_id]["pages"]

    if req.page_index < 0 or req.page_index >= len(pages):
        raise HTTPException(400, "Invalid page index.")

    page = pages[req.page_index]
    current = req.current_summary.strip()
    lang_name = LANGUAGE_NAMES[normalize_language(req.language)]

    is_comic = bool(page.get("image_data"))

    if is_comic:
        summary_prefix = f"Current summary:\n{current}\n\n" if current else ""
        content = []
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": page["media_type"], "data": page["image_data"]},
        })
        content.append({
            "type": "text",
            "text": (
                f"{summary_prefix}This is comic book page {page['page_num']}. "
                f"Write a 2–4 sentence plain, casual {lang_name} summary covering everything up to and including this page. "
                "Keep it casual, focused on the main story thread, and written like a quick catch-up for a friend. "
                "Return only the summary text, nothing else."
            ),
        })
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": content}],
        )
    else:
        summary_prefix = f"Current summary:\n{current}\n\n" if current else ""
        prompt = (
            f"{summary_prefix}"
            f"New page ({page['title']}):\n{page['text'][:2000]}\n\n"
            f"Write a 2–4 sentence plain, casual {lang_name} summary covering everything up to and including this page. "
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
    if req.page_index + 1 < len(pages):
        next_text = pages[req.page_index + 1]["text"]
        next_teaser = " ".join(next_text.split()[:30])

    return {"summary": msg.content[0].text.strip(), "next_teaser": next_teaser}


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
    return {"text": pages[page_index]}


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
