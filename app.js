// Podcast Clip Hub — frontend

const state = {
  clips: [],          // all clips from clips.json
  podcasts: [],       // podcast metadata from podcasts_meta.json
  selectedPodcast: 'all',
  filtered: [],
};

// ── Init ───────────────────────────────────────────────────────────────────────

async function initialize() {
  const [clipsRes, metaRes, configRes] = await Promise.allSettled([
    fetch('./clips.json').then(r => r.json()),
    fetch('./podcasts_meta.json').then(r => r.ok ? r.json() : Promise.reject()),
    fetch('./podcasts.json').then(r => r.json()),
  ]);

  state.clips = normalizeClips(clipsRes.status === 'fulfilled' ? clipsRes.value : []);

  if (metaRes.status === 'fulfilled' && metaRes.value.length) {
    // Best case: podcasts_meta.json exists with artwork
    state.podcasts = metaRes.value;
  } else if (configRes.status === 'fulfilled') {
    // Fallback: synthesize from podcasts.json config (no artwork yet)
    state.podcasts = (configRes.value.podcasts || [])
      .filter(p => p.enabled !== false)
      .map(p => ({ name: p.name, rss: p.rss || '', artwork_url: '', description: '' }));
  } else if (state.clips.length) {
    // Last resort: derive from clip data
    const seen = new Set();
    state.clips.forEach(c => {
      if (!seen.has(c.podcast_name)) {
        seen.add(c.podcast_name);
        state.podcasts.push({ name: c.podcast_name, rss: '', artwork_url: '', description: '' });
      }
    });
  }

  buildSidebar();
  populateFilters();
  bindEvents();

  // Handle deep link — may select a podcast or highlight a clip
  handleDeepLink();

  filterAndRender();
}

// ── Data normalization ─────────────────────────────────────────────────────────

function normalizeClips(raw) {
  return raw.map(c => ({
    id: c.id || '',
    podcast_name: (c.podcast_name || '').trim(),
    episode_title: (c.episode_title || '').trim(),
    episode_date: c.episode_date || '',
    episode_url: c.episode_url || '',
    episode_artwork: c.episode_artwork || '',
    transcript: (c.transcript || '').trim(),
    reason: (c.reason || '').trim(),
    topics: Array.isArray(c.topics) ? c.topics.filter(Boolean) : [],
    people: Array.isArray(c.people) ? c.people.filter(Boolean) : [],
    relevance_score: Number(c.relevance_score) || 0,
    clip_audio: c.clip_audio || '',
    start_time: Number(c.start_time) || 0,
    end_time: Number(c.end_time) || 0,
  }));
}

// ── Sidebar ────────────────────────────────────────────────────────────────────

function buildSidebar() {
  const nav = document.getElementById('podcastNav');
  nav.innerHTML = '';

  // Clip counts per podcast
  const counts = {};
  state.clips.forEach(c => { counts[c.podcast_name] = (counts[c.podcast_name] || 0) + 1; });

  // "All" row
  const allBtn = document.createElement('button');
  allBtn.className = 'podcast-row' + (state.selectedPodcast === 'all' ? ' active' : '');
  allBtn.dataset.podcast = 'all';
  allBtn.innerHTML = `
    <span class="podcast-row-icon all-icon">🎙</span>
    <span class="podcast-row-info">
      <span class="podcast-row-name">All podcasts</span>
      <span class="podcast-row-count">${state.clips.length} clip${state.clips.length !== 1 ? 's' : ''}</span>
    </span>`;
  allBtn.addEventListener('click', () => selectPodcast('all'));
  nav.appendChild(allBtn);

  const divider = document.createElement('div');
  divider.className = 'nav-divider';
  nav.appendChild(divider);

  // One row per podcast (ordered by clip count desc, then alpha)
  const pods = [...state.podcasts].sort((a, b) => {
    const diff = (counts[b.name] || 0) - (counts[a.name] || 0);
    return diff !== 0 ? diff : a.name.localeCompare(b.name);
  });

  pods.forEach(pod => {
    const count = counts[pod.name] || 0;
    const btn = document.createElement('button');
    btn.className = 'podcast-row' + (state.selectedPodcast === pod.name ? ' active' : '');
    btn.dataset.podcast = pod.name;

    const artworkHtml = pod.artwork_url
      ? `<img class="podcast-row-art" src="${escapeAttr(pod.artwork_url)}" alt="" loading="lazy" />`
      : `<span class="podcast-row-icon">🎙</span>`;

    btn.innerHTML = `
      ${artworkHtml}
      <span class="podcast-row-info">
        <span class="podcast-row-name">${escapeHtml(pod.name)}</span>
        <span class="podcast-row-count">${count} clip${count !== 1 ? 's' : ''}</span>
      </span>`;
    btn.addEventListener('click', () => selectPodcast(pod.name));
    nav.appendChild(btn);
  });
}

function selectPodcast(name) {
  state.selectedPodcast = name;
  // Update active state in sidebar
  document.querySelectorAll('.podcast-row').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.podcast === name);
  });
  filterAndRender();
}

// ── Filter dropdowns ───────────────────────────────────────────────────────────

function populateFilters() {
  const topics = [...new Set(state.clips.flatMap(c => c.topics).filter(Boolean))].sort();
  const people = [...new Set(state.clips.flatMap(c => c.people).filter(Boolean))].sort();
  fillSelect('topicFilter', topics);
  fillSelect('personFilter', people);
}

function fillSelect(id, values) {
  const sel = document.getElementById(id);
  const first = sel.options[0];
  sel.innerHTML = '';
  sel.appendChild(first);
  values.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  });
}

// ── Events ─────────────────────────────────────────────────────────────────────

function bindEvents() {
  const onChange = () => filterAndRender();
  document.getElementById('searchInput').addEventListener('input', onChange);
  document.getElementById('topicFilter').addEventListener('change', onChange);
  document.getElementById('personFilter').addEventListener('change', onChange);
  document.getElementById('sortSelect').addEventListener('change', onChange);

  // Sidebar toggle for mobile
  document.getElementById('sidebarToggle').addEventListener('click', () => {
    document.querySelector('.sidebar').classList.toggle('open');
  });
  // Close sidebar on outside click (mobile)
  document.addEventListener('click', e => {
    const sidebar = document.querySelector('.sidebar');
    if (sidebar.classList.contains('open') &&
        !sidebar.contains(e.target) &&
        !document.getElementById('sidebarToggle').contains(e.target)) {
      sidebar.classList.remove('open');
    }
  });
}

// ── Filter + sort ──────────────────────────────────────────────────────────────

function filterAndRender() {
  const query  = document.getElementById('searchInput').value.trim().toLowerCase();
  const topic  = document.getElementById('topicFilter').value;
  const person = document.getElementById('personFilter').value;
  const sort   = document.getElementById('sortSelect').value;

  state.filtered = state.clips.filter(c => {
    if (state.selectedPodcast !== 'all' && c.podcast_name !== state.selectedPodcast) return false;
    if (topic  && !c.topics.includes(topic))  return false;
    if (person && !c.people.includes(person)) return false;
    if (query) {
      const hay = [c.podcast_name, c.episode_title, c.transcript, c.reason, ...c.topics, ...c.people]
        .join(' ').toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });

  state.filtered.sort((a, b) => {
    if (sort === 'newest') return b.episode_date > a.episode_date ? 1 : -1;
    if (sort === 'oldest') return a.episode_date > b.episode_date ? 1 : -1;
    if (sort === 'score')  return b.relevance_score - a.relevance_score;
    return 0;
  });

  renderEpisodeGroups();
}

// ── Episode-grouped rendering ──────────────────────────────────────────────────

function renderEpisodeGroups() {
  const list   = document.getElementById('episodeList');
  const empty  = document.getElementById('emptyState');
  const emptyMsg = document.getElementById('emptyMsg');

  if (state.filtered.length === 0) {
    list.replaceChildren();
    empty.hidden = false;
    emptyMsg.textContent = state.clips.length === 0
      ? 'No clips yet. Run the pipeline to get started.'
      : 'No clips match your filters.';
    return;
  }
  empty.hidden = true;

  // Group by podcast_name + episode_title + episode_date
  const groups = groupByEpisode(state.filtered);
  const fragment = document.createDocumentFragment();

  groups.forEach(group => {
    const section = buildEpisodeSection(group);
    fragment.appendChild(section);
  });

  list.replaceChildren(fragment);
}

function groupByEpisode(clips) {
  const map = new Map();
  clips.forEach(c => {
    const key = `${c.podcast_name}|||${c.episode_date}|||${c.episode_title}`;
    if (!map.has(key)) {
      map.set(key, {
        podcast_name:   c.podcast_name,
        episode_title:  c.episode_title,
        episode_date:   c.episode_date,
        episode_url:    c.episode_url,
        episode_artwork: c.episode_artwork,
        clips: [],
      });
    }
    map.get(key).clips.push(c);
  });
  return [...map.values()];
}

function buildEpisodeSection(group) {
  const section = document.createElement('section');
  section.className = 'episode-section';

  // ── Episode header
  const header = document.createElement('div');
  header.className = 'episode-header';

  const artworkHtml = group.episode_artwork
    ? `<img class="episode-art" src="${escapeAttr(group.episode_artwork)}" alt="" loading="lazy" />`
    : `<div class="episode-art-placeholder">🎙</div>`;

  const podBadge = state.selectedPodcast === 'all'
    ? `<span class="episode-podcast-badge">${escapeHtml(group.podcast_name)}</span>`
    : '';

  const titleHtml = group.episode_url
    ? `<a href="${escapeAttr(group.episode_url)}" target="_blank" rel="noopener" class="episode-title-link">${escapeHtml(group.episode_title)}</a>`
    : `<span class="episode-title-text">${escapeHtml(group.episode_title)}</span>`;

  header.innerHTML = `
    ${artworkHtml}
    <div class="episode-header-info">
      ${podBadge}
      <h2 class="episode-title">${titleHtml}</h2>
      <div class="episode-meta">
        <time>${formatDate(group.episode_date)}</time>
        <span class="episode-clip-count">${group.clips.length} clip${group.clips.length !== 1 ? 's' : ''}</span>
      </div>
    </div>`;
  section.appendChild(header);

  // ── Clip cards grid
  const grid = document.createElement('div');
  grid.className = 'card-grid';
  const template = document.getElementById('clipCardTemplate');

  group.clips.forEach((clip, i) => {
    const node = template.content.cloneNode(true);
    const card = node.querySelector('.card');
    card.dataset.clipId = clip.id;
    card.style.animationDelay = `${Math.min(i * 30, 180)}ms`;

    node.querySelector('[data-field="transcript"]').textContent =
      clip.transcript ? `"${clip.transcript}"` : '';

    node.querySelector('[data-field="reason"]').textContent = clip.reason;

    const tagsEl = node.querySelector('[data-field="tags"]');
    clip.topics.forEach(t => {
      const s = document.createElement('span');
      s.className = 'tag topic-tag';
      s.textContent = t;
      tagsEl.appendChild(s);
    });
    clip.people.forEach(p => {
      const s = document.createElement('span');
      s.className = 'tag person-tag';
      s.textContent = p;
      tagsEl.appendChild(s);
    });

    const audio = node.querySelector('[data-field="clip_audio"]');
    if (clip.clip_audio) {
      audio.src = clip.clip_audio;
    } else {
      audio.closest('.card-audio-wrap').hidden = true;
    }

    const badge = node.querySelector('[data-field="relevance_score"]');
    if (clip.relevance_score) {
      badge.textContent = `${clip.relevance_score}/10`;
      badge.className = `relevance-badge score-${Math.min(10, clip.relevance_score)}`;
    } else {
      badge.hidden = true;
    }

    node.querySelector('.share-btn').addEventListener('click', () => shareClip(clip.id, card));
    grid.appendChild(node);
  });

  section.appendChild(grid);
  return section;
}

// ── Deep linking ───────────────────────────────────────────────────────────────

function handleDeepLink() {
  const params = new URLSearchParams(window.location.search);
  const clipId = params.get('clip');
  if (!clipId) return;

  // Find the clip's podcast and select it
  const clip = state.clips.find(c => c.id === clipId);
  if (clip) selectPodcast(clip.podcast_name);

  setTimeout(() => {
    const card = document.querySelector(`[data-clip-id="${CSS.escape(clipId)}"]`);
    if (!card) return;
    card.classList.add('highlighted');
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => card.classList.remove('highlighted'), 3000);
  }, 150);
}

// ── Share ──────────────────────────────────────────────────────────────────────

function shareClip(clipId, cardEl) {
  const url = new URL(window.location.href);
  url.search = '';
  url.searchParams.set('clip', clipId);

  const btn = cardEl.querySelector('.share-btn');
  const original = btn.innerHTML;

  navigator.clipboard.writeText(url.toString()).then(() => {
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.innerHTML = original; btn.classList.remove('copied'); }, 2000);
  }).catch(() => {
    const inp = document.createElement('input');
    inp.value = url.toString();
    document.body.appendChild(inp);
    inp.select();
    document.execCommand('copy');
    document.body.removeChild(inp);
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.innerHTML = original; }, 2000);
  });
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function formatDate(str) {
  if (!str) return '';
  try {
    return new Date(str + 'T00:00:00').toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric',
    });
  } catch { return str; }
}

function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escapeAttr(s) {
  return String(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Boot ───────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', initialize);
