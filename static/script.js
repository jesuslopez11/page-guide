const $ = id => document.getElementById(id);

const state = {
  contentId:     null,
  pages:         [],   // [{index, title, page_num}]
  overview:      '',   // generated once on upload — what this book is about
  currentIndex:  0,
  mode:          'short',
  language:      'english',
  cache:         {},   // `${index}-${mode}-${language}` => markdown string
  summaryCache:  {},   // pageIndex => summary after reading that page
  summary:       '',   // rolling "story so far" summary
  nextTeaser:    '',   // first few words of the next page
  streaming:     false,
  hasPageImages: false,
};

// ── Audio / TTS ────────────────────────────────────────────────────────────────
let playbackSpeed = 1.0;
let activeBtn = null;
let currentTTSAudio = null; // the <audio> element when playing ElevenLabs (Spanish) audio

// Voices load asynchronously in most browsers — cache them as soon as they're
// ready so picking a Spanish voice doesn't race an empty list on first use.
let cachedVoices = speechSynthesis.getVoices();
speechSynthesis.onvoiceschanged = () => { cachedVoices = speechSynthesis.getVoices(); };

function pickVoice(lang) {
  const voices = cachedVoices.length ? cachedVoices : speechSynthesis.getVoices();
  if (!voices.length) return null;
  const exact = voices.find(v => v.lang.toLowerCase() === lang.toLowerCase());
  if (exact) return exact;
  const base = lang.split('-')[0].toLowerCase();
  return voices.find(v => v.lang.toLowerCase().startsWith(base)) || null;
}

function stopAudio() {
  speechSynthesis.cancel();
  if (currentTTSAudio) {
    currentTTSAudio.pause();
    currentTTSAudio = null;
  }
  activeBtn = null;
  document.querySelectorAll('.tts-btn').forEach(btn => {
    btn.disabled = false;
    btn.dataset.playing = '';
    btn.textContent = btn.dataset.label;
  });
  const panel = document.getElementById('reading-panel');
  if (panel) panel.remove();
  document.getElementById('tts-error')?.remove();
}

function showTTSError(msg) {
  document.getElementById('tts-error')?.remove();
  const bar = document.getElementById('tts-bar');
  if (!bar) return;
  const el = document.createElement('div');
  el.id = 'tts-error';
  el.className = 'tts-error';
  el.textContent = `⚠️ ${msg}`;
  bar.parentNode.insertBefore(el, bar.nextSibling);
}

function showReadingPanel(text) {
  document.getElementById('reading-panel')?.remove();
  const panel = document.createElement('div');
  panel.id = 'reading-panel';
  panel.className = 'reading-panel';
  const words = text.trim().split(/\s+/);
  panel.innerHTML = words.map((w, i) =>
    `<span class="word" data-i="${i}">${w}</span>`
  ).join(' ');
  const bar = document.getElementById('tts-bar');
  bar.parentNode.insertBefore(panel, bar);
  return panel;
}

function highlightWord(panel, charIndex) {
  const spans = panel.querySelectorAll('.word');
  let count = 0;
  for (let i = 0; i < spans.length; i++) {
    spans[i].classList.remove('active');
    if (count <= charIndex && charIndex < count + spans[i].textContent.length + 1) {
      spans[i].classList.add('active');
      spans[i].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
    count += spans[i].textContent.length + 1;
  }
}

function speakText(btn, text) {
  if (btn.dataset.playing === '1') {
    if (speechSynthesis.paused) {
      speechSynthesis.resume();
      btn.textContent = '⏸ Pause';
    } else {
      speechSynthesis.pause();
      btn.textContent = '▶ Resume';
    }
    return;
  }

  stopAudio();
  const panel = showReadingPanel(text);
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = playbackSpeed;

  const lang = state.language === 'spanish' ? 'es-MX' : 'en-US';
  utterance.lang = lang;
  const voice = pickVoice(lang);
  if (voice) utterance.voice = voice;

  utterance.onboundary = e => {
    if (e.name === 'word') highlightWord(panel, e.charIndex);
  };

  utterance.onend = () => {
    panel.remove();
    btn.disabled = false;
    btn.dataset.playing = '';
    btn.textContent = btn.dataset.label;
    activeBtn = null;
  };

  utterance.onerror = () => {
    panel.remove();
    btn.disabled = false;
    btn.dataset.playing = '';
    btn.textContent = btn.dataset.label;
    activeBtn = null;
  };

  activeBtn = btn;
  btn.dataset.playing = '1';
  btn.textContent = '⏸ Pause';
  speechSynthesis.speak(utterance);
}

// Spanish read-aloud goes through the backend (ElevenLabs) instead of the
// browser's local voices, since most machines don't have a Spanish voice
// installed — English keeps using the free browser voices.
async function speakViaElevenLabs(btn, text) {
  if (btn.dataset.playing === '1') {
    if (currentTTSAudio) {
      if (currentTTSAudio.paused) {
        currentTTSAudio.play();
        btn.textContent = '⏸ Pause';
      } else {
        currentTTSAudio.pause();
        btn.textContent = '▶ Resume';
      }
    }
    return;
  }

  stopAudio();
  btn.disabled = true;
  btn.textContent = '⏳ Loading…';

  try {
    const res = await fetch('/tts', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ text, language: state.language }),
    });
    if (!res.ok) {
      let msg = 'Could not generate audio.';
      try { const err = await res.json(); msg = err.detail || msg; } catch {}
      throw new Error(msg);
    }

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.playbackRate = playbackSpeed;
    currentTTSAudio = audio;

    const cleanup = () => {
      URL.revokeObjectURL(url);
      btn.disabled = false;
      btn.dataset.playing = '';
      btn.textContent = btn.dataset.label;
      activeBtn = null;
      currentTTSAudio = null;
    };
    audio.onended = cleanup;
    audio.onerror = () => {
      cleanup();
      showTTSError('The audio ElevenLabs returned could not be played.');
    };

    activeBtn = btn;
    btn.dataset.playing = '1';
    btn.disabled = false;
    btn.textContent = '⏸ Pause';
    audio.play();
  } catch (err) {
    btn.disabled = false;
    btn.dataset.playing = '';
    btn.textContent = btn.dataset.label;
    showTTSError(err.message || 'Could not play audio.');
  }
}

function ttsPlay(btn, text) {
  speakText(btn, text);
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
  btnExp.onclick = () => ttsPlay(btnExp, explanationText);

  const btnPage = document.createElement('button');
  btnPage.className = 'tts-btn';
  btnPage.dataset.label = '📖 Read page text';
  btnPage.textContent   = '📖 Read page text';
  btnPage.onclick = async () => {
    if (btnPage.dataset.playing === '1') { ttsPlay(btnPage, ''); return; }
    btnPage.textContent = '⏳ Loading…';
    btnPage.disabled = true;
    try {
      const res = await fetch(`/page-text?content_id=${state.contentId}&page_index=${state.currentIndex}`);
      const data = await res.json();
      btnPage.disabled = false;
      btnPage.textContent = btnPage.dataset.label;
      ttsPlay(btnPage, data.text);
    } catch {
      btnPage.disabled = false;
      btnPage.textContent = btnPage.dataset.label;
    }
  };

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
    if (currentTTSAudio) currentTTSAudio.playbackRate = playbackSpeed;
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
const langBtns       = document.querySelectorAll('.lang-btn');
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
const urlInput       = $('url-input');
const urlBtn         = $('url-btn');

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

// Tracks an in-flight /upload or /upload-url call so switching language
// mid-upload can cancel it and restart immediately instead of waiting for
// the old language's extraction + overview generation to finish first.
let activeUploadController = null;
let pendingUpload = null; // { type: 'file', file } or { type: 'url', url }

async function upload(file) {
  pendingUpload = { type: 'file', file };
  dropZone.querySelector('.drop-label').textContent = 'Uploading…';
  if (activeUploadController) activeUploadController.abort();
  const controller = new AbortController();
  activeUploadController = controller;

  const fd = new FormData();
  fd.append('file', file);
  fd.append('language', state.language);
  try {
    const res = await fetch('/upload', { method: 'POST', body: fd, signal: controller.signal });
    if (!res.ok) {
      let msg = `Upload failed (${res.status})`;
      try { const err = await res.json(); msg = err.detail || msg; } catch {}
      throw new Error(msg);
    }
    pendingUpload = null;
    initBook(await res.json());
  } catch (err) {
    if (err.name === 'AbortError') return; // superseded by a language-switch retry
    dropZone.querySelector('.drop-label').textContent = `Error: ${err.message}`;
  }
}

urlBtn.addEventListener('click', () => {
  const url = urlInput.value.trim();
  if (url) uploadUrl(url);
});
urlInput.addEventListener('keydown', e => { if (e.key === 'Enter') urlBtn.click(); });

async function uploadUrl(url) {
  pendingUpload = { type: 'url', url };
  dropZone.querySelector('.drop-label').textContent = 'Fetching page…';
  urlBtn.disabled = true;
  if (activeUploadController) activeUploadController.abort();
  const controller = new AbortController();
  activeUploadController = controller;
  try {
    const res = await fetch('/upload-url', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ url, language: state.language }),
      signal:  controller.signal,
    });
    if (!res.ok) {
      let msg = `Fetch failed (${res.status})`;
      try { const err = await res.json(); msg = err.detail || msg; } catch {}
      throw new Error(msg);
    }
    pendingUpload = null;
    initBook(await res.json());
    urlInput.value = '';
  } catch (err) {
    if (err.name === 'AbortError') return; // superseded by a language-switch retry
    dropZone.querySelector('.drop-label').textContent = `Error: ${err.message}`;
  } finally {
    urlBtn.disabled = false;
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
  state.hasPageImages = data.has_page_images || data.is_comic || false;

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

langBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    langBtns.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.language = btn.dataset.lang;

    if (pendingUpload) {
      // The upload/fetch is still extracting + generating the overview in
      // the old language — cancel it and restart right away in the new one
      // instead of waiting for that to finish first.
      if (pendingUpload.type === 'file') upload(pendingUpload.file);
      else uploadUrl(pendingUpload.url);
    } else if (state.contentId) {
      loadPage(state.currentIndex, true);
    }
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
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') goToPage(state.currentIndex + 1);
  if (e.key === 'ArrowLeft'  || e.key === 'ArrowUp')   goToPage(state.currentIndex - 1);
});

function goToPage(index) {
  if (!state.pages.length) return;
  index = Math.max(0, Math.min(index, state.pages.length - 1));
  state.currentIndex = index;

  stopAudio(); // stop any playing audio when navigating

  // Restore the best available summary for context when jumping around —
  // reset first so a page with no earlier cached summary (e.g. jumping
  // back before anything you've read) doesn't inherit stale, later context.
  state.summary    = '';
  state.nextTeaser = '';
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
// Navigating away mid-stream cancels the old request instead of blocking the
// new one — activeLoadToken marks which call is still "current" so a
// superseded call's late-arriving data never overwrites the new page's view.
let activeLoadToken = 0;
let activeAbortController = null;

async function loadPage(index, forceRefresh) {
  const key = `${index}-${state.mode}-${state.language}`;

  // Whatever was previously loading is no longer wanted — cancel it so it
  // stops burning tokens and can't race with what we're about to show.
  if (activeAbortController) activeAbortController.abort();
  activeAbortController = null;
  const myToken = ++activeLoadToken;
  const isCurrent = () => myToken === activeLoadToken;

  if (!forceRefresh && state.cache[key]) {
    render(state.cache[key]);
    output.scrollTop = 0;
    return;
  }

  const controller = new AbortController();
  activeAbortController = controller;
  state.streaming = true;

  output.innerHTML = overviewHTML() + pageImageHTML(index) + `
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
        language:   state.language,
      }),
      signal: controller.signal,
    });
    if (!isCurrent()) return;

    if (!res.ok) {
      const err = await res.json();
      output.innerHTML = `<div class="error-box">${err.detail || 'Something went wrong.'}</div>`;
      return;
    }

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let accumulated = '';

    const div = document.createElement('div');
    div.className = 'explanation';
    output.innerHTML = overviewHTML() + pageImageHTML(index);
    output.appendChild(div);
    output.scrollTop = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (!isCurrent()) { reader.cancel(); return; }
      if (done) break;
      for (const line of decoder.decode(value).split('\n')) {
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6);
        if (payload === '[DONE]') break;
        try {
          const obj = JSON.parse(payload);
          if (obj.error) {
            div.innerHTML = `<div class="error-box">${obj.error}</div>`;
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
    if (err.name === 'AbortError') return; // superseded by a newer page load
    output.innerHTML = `<div class="error-box">${err.message}</div>`;
  } finally {
    if (isCurrent()) state.streaming = false;
  }
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
        language:        state.language,
      }),
    });
    if (res.ok) {
      const data = await res.json();
      state.summaryCache[pageIndex] = data.summary;

      // Only touch the live "story so far" context if the user hasn't
      // already navigated elsewhere while this request was in flight —
      // otherwise a late response would overwrite the correct context
      // for whatever page they're actually looking at now.
      if (pageIndex === state.currentIndex) {
        state.summary    = data.summary;
        state.nextTeaser = data.next_teaser || '';
        const box = document.getElementById('context-box');
        if (box) box.outerHTML = overviewHTML();
      }
    }
  } catch { /* fail silently — summary is nice-to-have, not critical */ }
}

function render(markdown) {
  const div = document.createElement('div');
  div.className = 'explanation';
  div.innerHTML = marked.parse(markdown);
  output.innerHTML = overviewHTML() + comicImageHTML(state.currentIndex);
  output.appendChild(div);
  addTTSBar(div.innerText);
}

function pageImageHTML(index) {
  if (!state.hasPageImages || !state.contentId) return '';
  const src = `/page-image?content_id=${state.contentId}&page_index=${index}`;
  return `<div class="page-image-wrap">
    <img class="page-image-img" src="${src}" alt="Page ${index + 1}"
         onclick="openPageImageLightbox('${src}')"
         onerror="this.style.display='none'; this.nextElementSibling.style.display='block'" />
    <p style="display:none;color:#888;text-align:center;padding:12px">⚠️ Page image could not be loaded</p>
  </div>`;
}

// Holds the cleanup for whichever lightbox is currently open, so Escape/close
// button/backdrop-click all tear down the same window-level drag listeners
// instead of just removing the DOM node and leaking them.
let closeLightbox = null;

function openPageImageLightbox(src) {
  closeLightbox?.();

  const overlay = document.createElement('div');
  overlay.id = 'page-image-lightbox';
  overlay.className = 'page-image-lightbox';

  const img = document.createElement('img');
  img.src = src;
  img.alt = 'Enlarged page';
  img.draggable = false;

  const closeBtn = document.createElement('button');
  closeBtn.className = 'lightbox-close';
  closeBtn.textContent = '✕';
  closeBtn.setAttribute('aria-label', 'Close');

  const zoomControls = document.createElement('div');
  zoomControls.className = 'lightbox-zoom-controls';
  const zoomOutBtn = document.createElement('button');
  zoomOutBtn.textContent = '−';
  zoomOutBtn.setAttribute('aria-label', 'Zoom out');
  const zoomLabel = document.createElement('span');
  zoomLabel.className = 'lightbox-zoom-label';
  const zoomInBtn = document.createElement('button');
  zoomInBtn.textContent = '+';
  zoomInBtn.setAttribute('aria-label', 'Zoom in');
  zoomControls.append(zoomOutBtn, zoomLabel, zoomInBtn);

  overlay.append(img, closeBtn, zoomControls);
  document.body.appendChild(overlay);

  const MIN_SCALE = 1, MAX_SCALE = 5, DBLCLICK_SCALE = 2.5;
  let scale = MIN_SCALE, originX = 0, originY = 0;
  let panning = false, panStartX = 0, panStartY = 0, originAtPanStartX = 0, originAtPanStartY = 0;
  let pinchStartDist = 0, pinchStartScale = 1;

  function clampOrigin() {
    // Keep the pan within the scaled image's bounds so it can't be dragged
    // fully off-screen with no way back.
    const maxX = (img.clientWidth  * (scale - 1)) / 2;
    const maxY = (img.clientHeight * (scale - 1)) / 2;
    originX = Math.min(maxX, Math.max(-maxX, originX));
    originY = Math.min(maxY, Math.max(-maxY, originY));
  }

  function render() {
    clampOrigin();
    img.style.transform = `translate(${originX}px, ${originY}px) scale(${scale})`;
    img.style.cursor = scale > MIN_SCALE ? 'grab' : 'zoom-in';
    zoomLabel.textContent = `${Math.round(scale * 100)}%`;
    zoomOutBtn.disabled = scale <= MIN_SCALE;
    zoomInBtn.disabled = scale >= MAX_SCALE;
  }

  function setScale(next) {
    scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next));
    if (scale === MIN_SCALE) { originX = 0; originY = 0; }
    render();
  }

  zoomInBtn.onclick  = e => { e.stopPropagation(); setScale(scale + 0.5); };
  zoomOutBtn.onclick = e => { e.stopPropagation(); setScale(scale - 0.5); };

  img.addEventListener('wheel', e => {
    e.preventDefault();
    setScale(scale + (e.deltaY < 0 ? 0.25 : -0.25));
  }, { passive: false });

  img.addEventListener('dblclick', e => {
    e.stopPropagation();
    setScale(scale > MIN_SCALE ? MIN_SCALE : DBLCLICK_SCALE);
  });

  img.addEventListener('mousedown', e => {
    if (scale <= MIN_SCALE) return;
    e.preventDefault();
    panning = true;
    panStartX = e.clientX; panStartY = e.clientY;
    originAtPanStartX = originX; originAtPanStartY = originY;
    img.style.cursor = 'grabbing';
  });
  const onMouseMove = e => {
    if (!panning) return;
    originX = originAtPanStartX + (e.clientX - panStartX);
    originY = originAtPanStartY + (e.clientY - panStartY);
    render();
  };
  const onMouseUp = () => { panning = false; render(); };
  window.addEventListener('mousemove', onMouseMove);
  window.addEventListener('mouseup', onMouseUp);

  function touchDist(touches) {
    const [a, b] = touches;
    return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  }
  img.addEventListener('touchstart', e => {
    if (e.touches.length === 2) {
      pinchStartDist = touchDist(e.touches);
      pinchStartScale = scale;
    } else if (e.touches.length === 1 && scale > MIN_SCALE) {
      panning = true;
      panStartX = e.touches[0].clientX; panStartY = e.touches[0].clientY;
      originAtPanStartX = originX; originAtPanStartY = originY;
    }
  }, { passive: true });
  img.addEventListener('touchmove', e => {
    if (e.touches.length === 2 && pinchStartDist) {
      e.preventDefault();
      setScale(pinchStartScale * (touchDist(e.touches) / pinchStartDist));
    } else if (e.touches.length === 1 && panning) {
      e.preventDefault();
      originX = originAtPanStartX + (e.touches[0].clientX - panStartX);
      originY = originAtPanStartY + (e.touches[0].clientY - panStartY);
      render();
    }
  }, { passive: false });
  img.addEventListener('touchend', () => { panning = false; pinchStartDist = 0; render(); });

  const onKeydown = e => { if (e.key === 'Escape') closeLightbox?.(); };
  document.addEventListener('keydown', onKeydown);

  overlay.addEventListener('click', e => {
    if (e.target === overlay) closeLightbox?.();
  });
  closeBtn.addEventListener('click', () => closeLightbox?.());

  closeLightbox = () => {
    window.removeEventListener('mousemove', onMouseMove);
    window.removeEventListener('mouseup', onMouseUp);
    document.removeEventListener('keydown', onKeydown);
    overlay.remove();
    closeLightbox = null;
  };

  render();
}

function overviewHTML() {
  if (state.summary) {
    // Once the user has read pages, show where they are + what's coming
    const teaser = state.nextTeaser
      ? `<span class="overview-next"><span class="overview-next-label">Coming up →</span>${state.nextTeaser}…</span>`
      : '';
    return `<div class="overview-box" id="context-box">
      <span class="overview-label">Where you are</span>
      <div class="overview-summary">${marked.parse(state.summary)}</div>
      ${teaser}
    </div>`;
  }
  if (state.overview) {
    // Before reading anything, show the book overview
    return `<div class="overview-box" id="context-box">
      <span class="overview-label">About this book</span>
      ${marked.parse(state.overview)}
    </div>`;
  }
  return '';
}
