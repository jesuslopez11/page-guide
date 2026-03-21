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


# ── Prompts ───────────────────────────────────────────────────────────────────

PROMPTS = {
    "short": """\
You are a friend who already read this book, helping someone who doesn't read much follow along page by page.

You may be given a "Story so far" summary, a "Previous page", and the "Current page".

Reply like this:
1. **Where you are** — one casual sentence placing this page in the bigger story. ONLY include if "Story so far" was provided.
2. **What just happened** — 2–3 plain sentences. When a character or concept appears, add a quick reminder in parentheses — e.g. "Paul (the teenage main character)".
3. **Bottom line** — one sentence. If you were texting a friend what this page was about, what would you say?

Under 130 words. Casual and warm. No jargon. No formal language.""",

    "medium": """\
You are a friend who already read this book, helping someone who doesn't read much follow along page by page. They struggle to connect ideas — make every link explicit and keep it casual.

You may be given a "Story so far" summary, a "Previous page", and the "Current page".

Talk to them like this:
1. **Where you are** — one casual sentence: where this page fits in the bigger story. ONLY include if "Story so far" was provided.
2. **From last page** — one sentence connecting the previous page to this one. ONLY include if "Previous page" was provided.
3. **What's happening** — walk through the main ideas in order, 3–5 short paragraphs. When any character or concept appears, remind them who/what it is in parentheses — e.g. "the Bene Gesserit (the secretive all-women order Jessica belongs to)".
4. **Why it matters** — one short paragraph: what is this page building toward?
5. **Watch for** — one casual sentence: what to look out for on the next page.

Write like you're sitting next to them. No jargon. No stiff language.""",

    "long": """\
You are a patient friend who already read this book carefully, helping someone who struggles with reading follow along page by page. They find it hard to connect ideas — leave no gap unexplained.

You may be given a "Story so far" summary, a "Previous page", and the "Current page".

Walk them through it like this:
1. **Where you are** — 1–2 casual sentences grounding them in the bigger story. What's been building, and where does this page fit in? ONLY include if "Story so far" was provided.
2. **From last page to this one** — exactly how the previous page flows into this one. ONLY include if "Previous page" was provided.
3. **Let's go through this** — walk through every idea on this page in order:
   - Say what's happening in plain English
   - Explain WHY — what is the author doing here? What's the purpose of this moment?
   - When any character or concept appears, remind them who/what it is — e.g. "Jessica (Paul's mom, trained by the Bene Gesserit)"
   - Connect each new idea to what came just before it on this page
4. **The big picture** — how does this page connect to the overall story being built?
5. **Bottom line** — one casual sentence. If you had to text a friend what this page was about, what would you say?

Warm, thorough, casual throughout. Like sitting right next to them.""",
}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in {".pdf", ".txt", ".md"}:
        raise HTTPException(400, f"Unsupported type '{suffix}'. Upload PDF, TXT, or MD.")

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
    else:
        text = raw.decode("utf-8", errors="ignore")
        pages = await asyncio.to_thread(extract_text_pages, text)

    if not pages:
        raise HTTPException(400, "No readable content found.")

    content_id = str(uuid.uuid4())
    content_store[content_id] = {"title": file.filename, "pages": pages}

    return {
        "content_id": content_id,
        "title": file.filename,
        "total_pages": len(pages),
        "last_page_num": pages[-1]["page_num"],
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

    if req.page_index < 0 or req.page_index >= len(pages):
        raise HTTPException(400, "Invalid page index.")

    page = pages[req.page_index]

    if len(page["text"].strip()) < 80:
        raise HTTPException(400, "This page has too little content to explain.")

    mode = req.mode if req.mode in PROMPTS else "medium"

    # Rolling story summary grounds the reader in the bigger picture
    summary_block = ""
    if req.summary.strip():
        summary_block = f'Story so far:\n"""\n{req.summary.strip()}\n"""\n\n'

    # Previous page text lets Claude bridge directly from last page
    prev_block = ""
    if req.page_index > 0:
        prev = pages[req.page_index - 1]
        prev_block = f'Previous page ({prev["title"]}):\n"""\n{prev["text"][:2500]}\n"""\n\n'

    user_msg = (
        f"{summary_block}"
        f"{prev_block}"
        f'Current page ({page["title"]}):\n"""\n{page["text"]}\n"""\n\n'
        "Please walk me through this page using your approach."
    )

    model = "claude-sonnet-4-6" if mode == "long" else "claude-haiku-4-5"

    def generate():
        try:
            with client.messages.stream(
                model=model,
                max_tokens=4000,
                system=PROMPTS[mode],
                messages=[{"role": "user", "content": user_msg}],
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

    prompt = (
        f"{'Current summary:\n' + current + chr(10) + chr(10) if current else ''}"
        f"New page ({page['title']}):\n{page['text'][:2000]}\n\n"
        f"Write a 2–4 sentence plain-English summary covering everything up to and including this page. "
        f"Keep it casual, focused on the main story thread, and written like a quick catch-up for a friend. "
        f"Return only the summary text, nothing else."
    )

    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=250,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"summary": msg.content[0].text.strip()}


@app.get("/health")
async def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
