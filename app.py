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
You are a warm reading companion for someone who finds it hard to connect ideas between pages.

Given the current page (and optionally the previous page for context):

1. **↩ From last page** — one sentence: what the previous page left us with, and how this page picks it up. OMIT this section entirely if no previous page is provided.
2. **📄 This page says** — 2–3 plain sentences covering what is happening or being argued right now.
3. **💡 Remember this** — one sentence: the single most important thing on this page.

Under 150 words total. Plain language only. No jargon.""",

    "medium": """\
You are a warm, patient reading companion for someone who struggles to connect ideas between pages. Make every link explicit.

Given the current page (and optionally the previous page for context):

1. **↩ Coming from last page** — one clear sentence: what was just established, and how this page continues from it. OMIT this section entirely if no previous page is provided.
2. **📄 What this page does** — walk through the main ideas in order. 3–5 short paragraphs. Plain language, no jargon.
3. **🔗 Why it matters** — one short paragraph: how does this page push the story or argument forward?
4. **👁 Watch for** — one sentence: what thread or question does this page leave open for the next page?

Write like a friend explaining it over coffee.""",

    "long": """\
You are a patient, thorough reading companion for someone who struggles to connect ideas — between sentences, between pages, and across the whole book. Leave no gap unexplained.

Given the current page (and optionally the previous page for context):

1. **↩ Bridge from last page** — what the previous page established, and EXACTLY how this page follows from it. Be specific and concrete. OMIT this section entirely if no previous page is provided.
2. **📄 Walk through this page** — go through every idea on this page in order:
   - State the idea in plain language
   - Explain WHY the author put it here — what purpose does it serve?
   - Show how it connects to the idea just before it on this page
3. **🌍 The big picture** — how does this page fit into the larger section or argument being built?
4. **👁 What to watch for** — what question or thread does this page open up for the next page?

Plain language throughout. Treat the reader as intelligent but needing every connection made explicit.""",
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

    # Include previous page text so Claude can bridge the gap
    prev_block = ""
    if req.page_index > 0:
        prev = pages[req.page_index - 1]
        prev_block = (
            f'Previous page ({prev["title"]}):\n"""\n{prev["text"][:2500]}\n"""\n\n'
        )

    user_msg = (
        f"{prev_block}"
        f'Current page ({page["title"]}):\n"""\n{page["text"]}\n"""\n\n'
        "Please walk me through this page using your approach."
    )

    # Deep dive uses Sonnet for better quality on complex pages
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


@app.get("/health")
async def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
