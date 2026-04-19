import os
import json
import uuid
import tempfile
import asyncio

import anthropic
from fastapi import FastAPI, UploadFile, File, HTTPException
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

client = anthropic.Anthropic()
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

    if ext == ".cbz":
        with zipfile.ZipFile(file_path) as zf:
            names = sorted([
                n for n in zf.namelist()
                if n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                and not os.path.basename(n).startswith(".")
            ])
            for name in names:
                image_bytes_list.append(zf.read(name))

    elif ext == ".cbr":
        import rarfile
        with rarfile.RarFile(file_path) as rf:
            names = sorted([
                n for n in rf.namelist()
                if n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                and not os.path.basename(n).startswith(".")
            ])
            for name in names:
                image_bytes_list.append(rf.read(name))

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


def extract_pdf_pages(file_path: str) -> list[dict]:
    import fitz
    doc = fitz.open(file_path)
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
    doc.close()
    return pages


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


def generate_book_overview(pages: list[dict]) -> str:
    """Quick one-time call on upload to understand what the book is about."""
    is_comic = bool(pages and pages[0].get("image_data"))

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
                "Plain English only. No markdown. No bullet points. No headers. No bold. Just plain sentences."
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
                    "Plain English only. No markdown. No bullet points. No headers. No bold. Just plain sentences."
                ),
            }],
        )
    return msg.content[0].text.strip()


# ── Prompts ───────────────────────────────────────────────────────────────────

PROMPTS = {
    "short": """\
You're a friend who already read this book, walking someone who doesn't read much through it page by page.

You're given context about the book, a running story summary (if available), the previous page (if available), and the current page.

Important: pages are physical PDF pages, so they often cut off mid-sentence. That's normal. If the page ends mid-thought, don't just stop — briefly say what the scene or moment was building toward, even if it wasn't finished on this page.

Write 3-4 short casual paragraphs. No headers. No bullet points. No bold labels. No section names. No "Bridge:" or "Bottom line:" prefixes. Just talk to them like you're sending a voice note.

Use the book overview and story summary to open with one grounding sentence — where are we in the story right now? Then explain what's happening on this page in plain English, slipping in character reminders naturally as they come up ("Paul, the main character" or "Jessica, his mom"). Let your last sentence land on what this page was building toward — even if it got cut off.

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
async def upload_file(file: UploadFile = File(...)):
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

    try:
        overview = await asyncio.to_thread(generate_book_overview, pages)
    except Exception:
        overview = ""

    content_id = str(uuid.uuid4())
    content_store[content_id] = {"title": file.filename, "pages": pages, "overview": overview}

    is_comic = suffix in {".cbr", ".cbz"}
    return {
        "content_id": content_id,
        "title": file.filename,
        "overview": overview,
        "total_pages": len(pages),
        "last_page_num": pages[-1]["page_num"],
        "is_comic": is_comic,
        "pages": [
            {"index": p["index"], "title": p["title"], "page_num": p["page_num"]}
            for p in pages
        ],
    }


class ExplainRequest(BaseModel):
    content_id: str
    page_index: int
    mode: str
    summary: str = ""  # rolling "story so far" built up as the user reads


class SummarizeRequest(BaseModel):
    content_id: str
    page_index: int
    current_summary: str = ""


@app.post("/explain")
async def explain(req: ExplainRequest):
    if req.content_id not in content_store:
        raise HTTPException(404, "Content not found. Please re-upload your file.")

    store = content_store[req.content_id]
    pages = store["pages"]
    overview = store.get("overview", "")

    if req.page_index < 0 or req.page_index >= len(pages):
        raise HTTPException(400, "Invalid page index.")

    page = pages[req.page_index]

    if len(page["text"].strip()) < 80:
        raise HTTPException(400, "This page has too little content to explain.")

    mode = req.mode if req.mode in PROMPTS else "medium"

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

    def generate():
        try:
            with client.messages.stream(
                model=model,
                max_tokens=4000,
                system=PROMPTS[mode],
                messages=[{"role": "user", "content": message_content}],
                timeout=120.0,
            ) as stream:
                for text in stream.text_stream:
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
                "Write a 2–4 sentence plain-English summary covering everything up to and including this page. "
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
        prompt = (
            f"{'Current summary:\n' + current + chr(10) + chr(10) if current else ''}"
            f"New page ({page['title']}):\n{page['text'][:2000]}\n\n"
            f"Write a 2–4 sentence plain-English summary covering everything up to and including this page. "
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


class TTSRequest(BaseModel):
    text: str = ""           # explanation text (sent from frontend)
    content_id: str = ""     # for reading raw page text
    page_index: int = -1     # for reading raw page text
    voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs Rachel (default)


@app.post("/tts")
async def text_to_speech(req: TTSRequest):
    import requests as req_lib
    from fastapi.responses import Response

    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise HTTPException(500, "ELEVENLABS_API_KEY is not set. Add it to your environment variables.")

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
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{req.voice_id}"
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
    if not page.get("image_data"):
        raise HTTPException(404, "This page has no image.")
    import base64
    image_bytes = base64.b64decode(page["image_data"])
    return Response(content=image_bytes, media_type=page.get("media_type", "image/jpeg"))


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
