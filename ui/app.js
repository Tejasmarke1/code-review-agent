/**
 * Code Review Agent — UI
 * Three-panel layout with live SSE agent trace.
 * Pure vanilla JS, no framework, no build step.
 */

const API = 'http://localhost:8000';
let currentEventSource = null;
let currentIssueCount  = 0;
let reviewStartTime    = null;

// ── Health check ───────────────────────────────────────────────────────────────

async function checkHealth() {
    try {
        const res  = await fetch(`${API}/health`, { signal: AbortSignal.timeout(5000) });
        const data = await res.json();
        const badge = document.getElementById('health-badge');
        badge.textContent = `${data.status} · neo4j=${data.neo4j_connected}`;
        badge.className   = `badge badge-${data.status}`;
        document.getElementById('uptime').textContent =
            `up ${formatSeconds(data.uptime_seconds)} · ${data.total_reviews_this_session} reviews`;
    } catch {
        document.getElementById('health-badge').textContent = 'offline';
        document.getElementById('health-badge').className   = 'badge badge-unhealthy';
        document.getElementById('uptime').textContent = '';
    }
}

// ── Streaming review ───────────────────────────────────────────────────────────

function startStreamingReview() {
    const repoUrl = document.getElementById('repo-url').value.trim();
    if (!repoUrl) { alert('Please enter a repository URL.'); return; }
    if (!repoUrl.startsWith('http')) { alert('URL must start with http:// or https://'); return; }

    const filesRaw  = document.getElementById('files-input').value.trim();
    const files     = filesRaw
        ? filesRaw.split('\n').map(f => f.trim()).filter(Boolean).join(',')
        : '';
    const maxFiles  = document.getElementById('max-files').value || 2;
    const useMemory = document.getElementById('use-memory').checked;

    resetTrace();
    currentIssueCount = 0;
    reviewStartTime   = Date.now();
    updateIssueCount(0);

    const params = new URLSearchParams({
        repo_url:   repoUrl,
        files:      files,
        max_files:  maxFiles,
        use_memory: useMemory,
    });

    document.getElementById('stream-btn').disabled = true;
    document.getElementById('stop-btn').disabled   = false;
    setTraceStatus('&#x1F504; Connecting to agent...', 'running');

    currentEventSource = new EventSource(`${API}/stream/review?${params}`);

    currentEventSource.addEventListener('status', e => {
        handleStatusEvent(JSON.parse(e.data));
    });
    currentEventSource.addEventListener('step', e => {
        renderTraceStep(JSON.parse(e.data));
    });
    currentEventSource.addEventListener('issue', e => {
        const d = JSON.parse(e.data);
        renderLiveIssue(d);
        currentIssueCount++;
        updateIssueCount(currentIssueCount);
    });
    currentEventSource.addEventListener('error', e => {
        try { renderTraceError(JSON.parse(e.data).message); } catch { renderTraceError('Connection error'); }
    });
    currentEventSource.addEventListener('done', e => {
        handleDoneEvent(JSON.parse(e.data));
        closeStream();
    });
    currentEventSource.onerror = () => {
        if (currentEventSource && currentEventSource.readyState === EventSource.CLOSED) {
            setTraceStatus('&#x26A0; Connection lost', 'error');
            closeStream();
        }
    };
}

function stopReview() {
    if (currentEventSource) {
        currentEventSource.close();
        currentEventSource = null;
        setTraceStatus('&#x23F9; Stopped by user', 'stopped');
    }
    document.getElementById('stream-btn').disabled = false;
    document.getElementById('stop-btn').disabled   = true;
}

function closeStream() {
    if (currentEventSource) { currentEventSource.close(); currentEventSource = null; }
    document.getElementById('stream-btn').disabled = false;
    document.getElementById('stop-btn').disabled   = true;
}

// ── Trace rendering ────────────────────────────────────────────────────────────

function resetTrace() {
    document.getElementById('agent-trace').innerHTML = '';
    document.getElementById('issues-live').innerHTML =
        '<p class="placeholder-text">Issues appear here as the agent finds them.</p>';
    document.getElementById('session-summary').classList.add('hidden');
    setTraceStatus('Waiting...', 'waiting');
}

function renderTraceStep(data) {
    const trace = document.getElementById('agent-trace');
    const ph    = trace.querySelector('.trace-placeholder');
    if (ph) ph.remove();

    const cat     = getToolCategory(data.action);
    const elapsed = reviewStartTime ? `${((Date.now() - reviewStartTime) / 1000).toFixed(1)}s` : '';

    const el = document.createElement('div');
    el.className = 'trace-step trace-enter';
    el.innerHTML = `
        <div class="step-header">
            <span class="step-num">Step ${(data.step_number ?? 0) + 1}</span>
            <span class="step-file">${escapeHtml(getFileName(data.file_path))}</span>
            <span class="step-time">${elapsed}</span>
        </div>
        <div class="step-row">
            <span class="step-tag tag-thought">Thought</span>
            <span class="thought-text">${escapeHtml((data.thought || '...').slice(0, 200))}</span>
        </div>
        <div class="step-row">
            <span class="step-tag tag-action tool-${cat}">${escapeHtml(data.action || '')}</span>
            <code class="action-code">${escapeHtml(formatInput(data.action_input))}</code>
        </div>
        ${data.observation_preview ? `
        <div class="step-row">
            <span class="step-tag tag-obs">Obs</span>
            <code class="obs-code">${escapeHtml(data.observation_preview.slice(0, 180))}...</code>
        </div>` : ''}
    `;
    trace.appendChild(el);
    trace.scrollTop = trace.scrollHeight;
    requestAnimationFrame(() => el.classList.remove('trace-enter'));
}

function renderLiveIssue(data) {
    const container = document.getElementById('issues-live');
    const ph = container.querySelector('.placeholder-text');
    if (ph) ph.remove();

    const el = document.createElement('div');
    el.className = `live-issue issue-${(data.severity || 'low').toLowerCase()} live-enter`;
    el.innerHTML = `
        <div class="live-issue-header">
            <span class="sev-badge sev-${(data.severity || '').toLowerCase()}">${data.severity}</span>
            <span class="issue-fname">${escapeHtml(getFileName(data.file_path))}</span>
            ${data.line_number != null ? `<span class="line-num">L${data.line_number}</span>` : ''}
        </div>
        <div class="live-issue-title">${escapeHtml(data.title)}</div>
    `;
    container.prepend(el);
    requestAnimationFrame(() => el.classList.remove('live-enter'));
}

function renderTraceError(msg) {
    const trace = document.getElementById('agent-trace');
    const el = document.createElement('div');
    el.className = 'trace-error';
    el.textContent = `⚠ ${msg}`;
    trace.appendChild(el);
    trace.scrollTop = trace.scrollHeight;
}

function addTraceMarker(text) {
    const trace = document.getElementById('agent-trace');
    const el = document.createElement('div');
    el.className = 'trace-marker';
    el.textContent = text;
    trace.appendChild(el);
    trace.scrollTop = trace.scrollHeight;
}

function handleStatusEvent(data) {
    if (data.type === 'session_started') {
        setTraceStatus(`🔄 Session ${data.session_id} — reviewing...`, 'running');
    } else if (data.type === 'file_complete') {
        addTraceMarker(`✓ ${getFileName(data.file_path || '')} complete — ${data.issues_count || 0} issues`);
    }
}

function handleDoneEvent(data) {
    if (data.error) {
        setTraceStatus(`❌ Failed: ${data.error}`, 'error');
        return;
    }
    const elapsed = reviewStartTime
        ? `${((Date.now() - reviewStartTime) / 1000).toFixed(1)}s`
        : `${(data.elapsed_seconds || 0).toFixed(1)}s`;

    setTraceStatus(
        `✅ Complete — ${data.files_reviewed}f, ${data.total_issues}i, ${elapsed}`,
        'done'
    );

    document.getElementById('session-summary').classList.remove('hidden');
    document.getElementById('summary-content').innerHTML = `
        <div class="sum-row"><span>Files reviewed</span><strong>${data.files_reviewed}</strong></div>
        <div class="sum-row"><span>Total issues</span><strong>${data.total_issues}</strong></div>
        <div class="sum-row"><span>Critical</span><strong class="sev-critical-txt">${data.critical_issues || 0}</strong></div>
        <div class="sum-row"><span>High</span><strong class="sev-high-txt">${data.high_issues || 0}</strong></div>
        <div class="sum-row"><span>Patterns</span><strong>${data.patterns || 0}</strong></div>
        <div class="sum-row"><span>Time</span><strong>${elapsed}</strong></div>
        ${data.session_id ? `<div class="sum-row"><span>Session</span><code>${data.session_id}</code></div>` : ''}
    `;

    addToSessionHistory(data);
    loadMemoryStats();
}

// ── Memory stats ───────────────────────────────────────────────────────────────

async function loadMemoryStats() {
    const panel = document.getElementById('memory-stats');
    try {
        const res  = await fetch(`${API}/memory/stats`);
        const data = await res.json();

        if (!data.connected) {
            panel.innerHTML = '<p class="warning">Neo4j not connected</p>';
            return;
        }

        const rows = Object.entries(data.node_counts)
            .sort(([, a], [, b]) => b - a)
            .map(([label, count]) =>
                `<div class="stat-row"><span class="stat-lbl">${label}</span><span class="stat-val">${count}</span></div>`)
            .join('');

        panel.innerHTML = `
            <div class="stats-grid">${rows}</div>
            <p class="stats-footer">${data.total_relationships} rels &bull; ${data.total_patterns} patterns</p>
        `;
    } catch {
        panel.innerHTML = '<p class="warning">Could not load stats</p>';
    }
}

// ── Session history ────────────────────────────────────────────────────────────

function addToSessionHistory(data) {
    const list = document.getElementById('session-list');
    const item = document.createElement('div');
    item.className = 'history-item';
    item.innerHTML = `
        <span class="hist-id">${data.session_id || 'unknown'}</span>
        <span class="hist-stats">${data.files_reviewed || 0}f &bull; ${data.total_issues || 0}i</span>
    `;
    list.prepend(item);
    while (list.children.length > 5) list.removeChild(list.lastChild);
}

// ── Utilities ──────────────────────────────────────────────────────────────────

function setTraceStatus(html, state) {
    const el = document.getElementById('trace-status');
    el.innerHTML = html;
    el.className = `trace-status status-${state}`;
}

function updateIssueCount(n) {
    document.getElementById('issue-count').textContent = n;
}

function getFileName(path) {
    if (!path) return '';
    return path.split(/[/\\]/).pop() || path;
}

function getToolCategory(name) {
    const map = {
        read_file: 'file', list_python_files: 'file', get_function_context: 'file',
        run_ruff: 'analysis', run_bandit: 'analysis', run_radon: 'analysis', check_imports: 'analysis',
        search_past_issues: 'memory', get_file_review_history: 'memory', get_repo_patterns: 'memory',
        clone_github_repo: 'github', list_github_files: 'github', get_github_file_path: 'github',
        cleanup_github_clone: 'github', finish_review: 'control',
    };
    return map[name] || 'other';
}

function formatInput(input) {
    if (!input) return '';
    const s = typeof input === 'string' ? input : JSON.stringify(input);
    return s.length > 90 ? s.slice(0, 90) + '...' : s;
}

function formatSeconds(s) {
    if (s < 60) return `${Math.round(s)}s`;
    return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Init ───────────────────────────────────────────────────────────────────────
checkHealth();
loadMemoryStats();
setInterval(checkHealth, 30000);