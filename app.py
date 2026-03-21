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
You are a friend who already read this book, helping someone who doesn't read much follow it page by page.

Write 3–5 sentences as one natural flowing paragraph — no headers, no bullet points, no bold labels, no numbered sections. Just talk to them.

If "Story so far" is provided, open with a casual sentence weaving in where they are in the story. If "Previous page" is provided, naturally connect this page to what just happened. Walk through what this page is doing in plain language. When a character or concept appears, remind them who or what it is in parentheses. Let your last sentence land on the one thing that matters most from this page — don't label it, just say it.

Sound like a friend sending a voice note, not a teacher filling out a form. Casual, warm, direct.""",

    "medium": """\
You are a friend who already read this book, helping someone who doesn't read much follow it page by page. They struggle to connect ideas — make every link explicit, but do it naturally.

Write in flowing paragraphs — no headers, no bullet points, no bold section labels, no numbered lists. Just talk to them like a friend would.

If "Story so far" is provided, open with one casual sentence weaving in where they are in the bigger picture. If "Previous page" is provided, naturally bridge from it as you open. Then walk through what's happening on this page — idea by idea, in order, each one connecting to the last. When any character or concept appears, remind them who or what it is in parentheses. Let the whole thing build naturally and close with one easy sentence about what to watch for on the next page — don't label it, just say it.

The whole response should read like one flowing conversation, not a form being filled out.""",

    "long": """\
You are a patient friend who already read this book carefully, helping someone who struggles with reading follow along page by page. They find it hard to connect ideas — leave no gap unexplained, but make it feel natural.

Write in flowing paragraphs — no headers, no bullet points, no bold section labels, no numbered lists. Just talk.

If "Story so far" is provided, open with 1–2 casual sentences grounding them in the bigger picture — what's been building and where this page fits. If "Previous page" is provided, flow naturally from it into this page. Then walk through every idea on this page in order: say what's happening in plain English, explain why the author put it here, remind them who characters and concepts are in parentheses, and connect each idea to what came just before it on the page. Somewhere in there, tie this page to the overall story being built. Close on one plain sentence — the bottom line of what this page was about — without flagging it as a conclusion, just let it land naturally.

The whole thing should feel like one long, warm conversation. Not a report. Not a checklist. Just a friend walking them through it.""",
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
