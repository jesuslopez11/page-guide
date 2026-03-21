const $ = id => document.getElementById(id);

const state = {
  contentId:    null,
  pages:        [],   // [{index, title, page_num}]
  currentIndex: 0,
  mode:         'medium',
  cache:        {},   // `${index}-${mode}` => markdown string
  streaming:    false,
};

// ── DOM refs ──────────────────────────────────────────────────────────────────
const dropZone    = $('drop-zone');
const fileInput   = $('file-input');
const bookSection = $('book-section');
const bookTitle   = $('book-title');
const bookStats   = $('book-stats');
const jumpInput   = $('jump-input');
const jumpBtn     = $('jump-btn');
const nearbyPages = $('nearby-pages');
const depthBtns   = document.querySelectorAll('.depth-btn');
const reExplain   = $('re-explain');
const pageNav     = $('page-nav');
const pageLabel   = $('page-label');
const prevBtn     = $('prev-btn');
const nextBtn     = $('next-btn');
const output      = $('output');
const bottomNav   = $('bottom-nav');
const prevBottom  = $('prev-bottom');
const nextBottom  = $('next-bottom');

// ── Upload ────────────────────────────────────────────────────────────────────
dropZone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', e => { if (e.target.files[0]) upload(e.target.files[0]); });

dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', ()  => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
});

async function upload(file) {
  dropZone.querySelector('.drop-label').textContent = 'Uploading…';
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/upload', { method: 'POST', body: fd });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Upload failed');
    }
    initBook(await res.json());
  } catch (err) {
    dropZone.querySelector('.drop-label').textContent = `Error: ${err.message}`;
  }
}

function initBook(data) {
  state.contentId    = data.content_id;
  state.pages        = data.pages;
  state.currentIndex = 0;
  state.cache        = {};

  bookTitle.textContent = data.title;
  bookStats.textContent = `${data.total_pages} readable pages`;
  jumpInput.max = data.last_page_num;

  bookSection.hidden = false;
  pageNav.hidden     = false;
  bottomNav.hidden   = false;
  reExplain.hidden   = false;

  dropZone.querySelector('.drop-label').textContent = 'Drop another file';

  goToPage(0);
}

// ── Depth selector ────────────────────────────────────────────────────────────
depthBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    depthBtns.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.mode = btn.dataset.mode;
    loadPage(state.currentIndex, true);
  });
});

reExplain.addEventListener('click', () => loadPage(state.currentIndex, true));

// ── Navigation ────────────────────────────────────────────────────────────────
prevBtn.addEventListener('click',    () => goToPage(state.currentIndex - 1));
nextBtn.addEventListener('click',    () => goToPage(state.currentIndex + 1));
prevBottom.addEventListener('click', () => goToPage(state.currentIndex - 1));
nextBottom.addEventListener('click', () => goToPage(state.currentIndex + 1));

jumpBtn.addEventListener('click', () => {
  const target = parseInt(jumpInput.value);
  if (isNaN(target)) return;
  // Find the first readable page at or after the requested page number
  const found = state.pages.findIndex(p => p.page_num >= target);
  if (found !== -1) goToPage(found);
});
jumpInput.addEventListener('keydown', e => { if (e.key === 'Enter') jumpBtn.click(); });

document.addEventListener('keydown', e => {
  if (state.streaming || e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') goToPage(state.currentIndex + 1);
  if (e.key === 'ArrowLeft'  || e.key === 'ArrowUp')   goToPage(state.currentIndex - 1);
});

function goToPage(index) {
  if (!state.pages.length) return;
  index = Math.max(0, Math.min(index, state.pages.length - 1));
  state.currentIndex = index;
  updateNav();
  updateNearbyPages();
  loadPage(index, false);
}

function updateNav() {
  const i     = state.currentIndex;
  const total = state.pages.length;
  const p     = state.pages[i];
  const last  = state.pages[total - 1];

  pageLabel.textContent = `Page ${p.page_num} of ${last.page_num}`;
  prevBtn.disabled    = i === 0;
  nextBtn.disabled    = i === total - 1;
  prevBottom.disabled = i === 0;
  nextBottom.disabled = i === total - 1;
}

function updateNearbyPages() {
  const i     = state.currentIndex;
  const start = Math.max(0, i - 3);
  const end   = Math.min(state.pages.length - 1, i + 3);

  nearbyPages.innerHTML = '';
  for (let j = start; j <= end; j++) {
    const p  = state.pages[j];
    const el = document.createElement('div');
    el.className = 'page-item' + (j === i ? ' active' : '');
    el.textContent = p.title;
    el.addEventListener('click', () => goToPage(j));
    nearbyPages.appendChild(el);
  }
}

// ── Explanation ───────────────────────────────────────────────────────────────
async function loadPage(index, forceRefresh) {
  const key = `${index}-${state.mode}`;

  if (!forceRefresh && state.cache[key]) {
    render(state.cache[key]);
    output.scrollTop = 0;
    return;
  }

  if (state.streaming) return;
  state.streaming = true;

  output.innerHTML = `
    <div class="loading-wrap">
      <div class="loading-dots"><span></span><span></span><span></span></div>
    </div>`;

  try {
    const res = await fetch('/explain', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        content_id: state.contentId,
        page_index: index,
        mode:       state.mode,
      }),
    });

    if (!res.ok) {
      const err = await res.json();
      output.innerHTML = `<div class="error-box">${err.detail || 'Something went wrong.'}</div>`;
      state.streaming = false;
      return;
    }

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let accumulated = '';

    const div = document.createElement('div');
    div.className = 'explanation';
    output.innerHTML = '';
    output.appendChild(div);
    output.scrollTop = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const line of decoder.decode(value).split('\n')) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6);
        if (payload === '[DONE]') { state.streaming = false; break; }
        try {
          const obj = JSON.parse(payload);
          if (obj.error) {
            div.innerHTML = `<div class="error-box">${obj.error}</div>`;
            state.streaming = false;
            return;
          }
          accumulated += obj.text;
          div.innerHTML = marked.parse(accumulated);
        } catch { /* ignore partial JSON */ }
      }
    }

    state.cache[key] = accumulated;
  } catch (err) {
    output.innerHTML = `<div class="error-box">${err.message}</div>`;
  }

  state.streaming = false;
}

function render(markdown) {
  const div = document.createElement('div');
  div.className = 'explanation';
  div.innerHTML = marked.parse(markdown);
  output.innerHTML = '';
  output.appendChild(div);
}
