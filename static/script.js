const $ = id => document.getElementById(id);

const state = {
  contentId:     null,
  pages:         [],   // [{index, title, page_num}]
  overview:      '',   // generated once on upload — what this book is about
  currentIndex:  0,
  mode:          'short',
  cache:         {},   // `${index}-${mode}` => markdown string
  summaryCache:  {},   // pageIndex => summary after reading that page
  summary:       '',   // rolling "story so far" summary
  nextTeaser:    '',   // first few words of the next page
  streaming:     false,
};

// ── Audio / TTS ────────────────────────────────────────────────────────────────
let currentAudio = null;
let playbackSpeed = 1.0;

function stopAudio() {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio.src = '';
    currentAudio = null;
  }
  document.querySelectorAll('.tts-btn').forEach(btn => {
    btn.disabled = false;
    btn.dataset.playing = '';
    btn.textContent = btn.dataset.label;
  });
}

async function speak(btn, fetchBody) {
  // Toggle pause/resume if this button is already playing
  if (currentAudio && btn.dataset.playing === '1') {
    if (currentAudio.paused) {
      currentAudio.play();
      btn.textContent = '⏸ Pause';
    } else {
      currentAudio.pause();
      btn.textContent = '▶ Resume';
    }
    return;
  }

  stopAudio();
  btn.textContent = '⏳ Loading…';
  btn.disabled = true;

  try {
    const res = await fetch('/tts', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(fetchBody),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'TTS failed');
    }

    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    currentAudio = new Audio(url);
    currentAudio.playbackRate = playbackSpeed;

    currentAudio.onended = () => {
      URL.revokeObjectURL(url);
      currentAudio = null;
      btn.disabled = false;
      btn.dataset.playing = '';
      btn.textContent = btn.dataset.label;
    };

    currentAudio.play();
    btn.dataset.playing = '1';
    btn.disabled = false;
    btn.textContent = '⏸ Pause';

  } catch (err) {
    btn.disabled = false;
    btn.textContent = btn.dataset.label;
    alert(`Read aloud error: ${err.message}`);
  }
}

function addTTSBar(explanationText) {
  document.getElementById('tts-bar')?.remove();
  const bar = document.createElement('div');
  bar.id = 'tts-bar';
  bar.className = 'tts-bar';

  const btnExp = document.createElement('button');
  btnExp.className = 'tts-btn';
  btnExp.dataset.label = '🔊 Read explanation';
  btnExp.textContent   = '🔊 Read explanation';
  btnExp.onclick = () => speak(btnExp, { text: explanationText });

  const btnPage = document.createElement('button');
  btnPage.className = 'tts-btn';
  btnPage.dataset.label = '📖 Read page text';
  btnPage.textContent   = '📖 Read page text';
  btnPage.onclick = () => speak(btnPage, {
    content_id: state.contentId,
    page_index: state.currentIndex,
  });

  const speedControl = document.createElement('div');
  speedControl.className = 'speed-control';

  const speedLabel = document.createElement('span');
  speedLabel.className = 'speed-label';
  speedLabel.textContent = `${playbackSpeed.toFixed(1)}x`;

  const slider = document.createElement('input');
  slider.type = 'range';
  slider.min = '0.5';
  slider.max = '2';
  slider.step = '0.25';
  slider.value = playbackSpeed;
  slider.className = 'speed-slider';
  slider.oninput = () => {
    playbackSpeed = parseFloat(slider.value);
    speedLabel.textContent = `${playbackSpeed.toFixed(1)}x`;
    if (currentAudio) currentAudio.playbackRate = playbackSpeed;
  };

  speedControl.appendChild(slider);
  speedControl.appendChild(speedLabel);

  bar.appendChild(btnExp);
  bar.appendChild(btnPage);
  bar.appendChild(speedControl);
  output.appendChild(bar);
}

// ── DOM refs ──────────────────────────────────────────────────────────────────
const dropZone       = $('drop-zone');
const fileInput      = $('file-input');
const bookSection    = $('book-section');
const bookTitle      = $('book-title');
const bookStats      = $('book-stats');
const jumpInput      = $('jump-input');
const jumpBtn        = $('jump-btn');
const nearbyPages    = $('nearby-pages');
const depthBtns      = document.querySelectorAll('.depth-btn');
const reExplain      = $('re-explain');
const pageNav        = $('page-nav');
const pageLabel      = $('page-label');
const prevBtn        = $('prev-btn');
const nextBtn        = $('next-btn');
const output         = $('output');
const bottomNav      = $('bottom-nav');
const prevBottom     = $('prev-bottom');
const nextBottom     = $('next-bottom');
const menuBtn        = $('menu-btn');
const sidebar        = $('sidebar');
const sidebarOverlay = $('sidebar-overlay');

// ── Mobile sidebar ────────────────────────────────────────────────────────────
function openSidebar()  {
  sidebar.classList.add('open');
  sidebarOverlay.classList.add('visible');
}
function closeSidebar() {
  sidebar.classList.remove('open');
  sidebarOverlay.classList.remove('visible');
}
menuBtn.addEventListener('click', () =>
  sidebar.classList.contains('open') ? closeSidebar() : openSidebar()
);
sidebarOverlay.addEventListener('click', closeSidebar);

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
  state.overview     = data.overview || '';
  state.currentIndex = 0;
  state.cache        = {};
  state.summaryCache = {};
  state.summary      = '';

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

  stopAudio(); // stop any playing audio when navigating

  // Restore the best available summary for context when jumping around
  for (let i = index - 1; i >= 0; i--) {
    if (state.summaryCache[i]) { state.summary = state.summaryCache[i]; break; }
  }

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
    el.addEventListener('click', () => { goToPage(j); closeSidebar(); });
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

  output.innerHTML = overviewHTML() + `
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
        summary:    state.summary,
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
    output.innerHTML = overviewHTML();
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

    // Add TTS buttons below the explanation
    addTTSBar(div.innerText);

    // Update the rolling summary in the background after the page is read
    updateSummary(index);
  } catch (err) {
    output.innerHTML = `<div class="error-box">${err.message}</div>`;
  }

  state.streaming = false;
}

async function updateSummary(pageIndex) {
  try {
    const res = await fetch('/summarize', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        content_id:      state.contentId,
        page_index:      pageIndex,
        current_summary: state.summary,
      }),
    });
    if (res.ok) {
      const data = await res.json();
      state.summary    = data.summary;
      state.nextTeaser = data.next_teaser || '';
      state.summaryCache[pageIndex] = data.summary;
      // Refresh the context box live without re-rendering the whole page
      const box = document.getElementById('context-box');
      if (box) box.outerHTML = overviewHTML();
    }
  } catch { /* fail silently — summary is nice-to-have, not critical */ }
}

function render(markdown) {
  const div = document.createElement('div');
  div.className = 'explanation';
  div.innerHTML = marked.parse(markdown);
  output.innerHTML = overviewHTML();
  output.appendChild(div);
  addTTSBar(div.innerText);
}

function overviewHTML() {
  if (state.summary) {
    // Once the user has read pages, show where they are + what's coming
    const teaser = state.nextTeaser
      ? `<span class="overview-next"><span class="overview-next-label">Coming up →</span>${state.nextTeaser}…</span>`
      : '';
    return `<div class="overview-box" id="context-box">
      <span class="overview-label">Where you are</span>
      <span class="overview-summary">${state.summary}</span>
      ${teaser}
    </div>`;
  }
  if (state.overview) {
    // Before reading anything, show the book overview
    return `<div class="overview-box" id="context-box">
      <span class="overview-label">About this book</span>
      ${state.overview}
    </div>`;
  }
  return '';
}
