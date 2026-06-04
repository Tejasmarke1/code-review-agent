/**
 * Code Review Agent — Frontend
 * Pure vanilla JS, no framework, no build step.
 * Talks to FastAPI backend at localhost:8000.
 */

const API = 'http://localhost:8000';
let currentSession = null;
let statusTimerInterval = null;
let statusStartTime = 0;

// ── Health check ───────────────────────────────────────────────────────────────

async function checkHealth() {
    try {
        const res = await fetch(`${API}/health`, { signal: AbortSignal.timeout(5000) });
        const data = await res.json();

        const badge = document.getElementById('health-badge');
        badge.textContent = data.status;
        badge.className = `badge badge-${data.status}`;

        const uptime = document.getElementById('uptime');
        uptime.textContent = `up ${formatSeconds(data.uptime_seconds)} · ${data.total_reviews_this_session} reviews`;
    } catch {
        const badge = document.getElementById('health-badge');
        badge.textContent = 'offline';
        badge.className = 'badge badge-unhealthy';
        document.getElementById('uptime').textContent = '';
    }
}

// ── Review submission ──────────────────────────────────────────────────────────

async function startReview() {
    const repoUrl = document.getElementById('repo-url').value.trim();
    if (!repoUrl) {
        showError('Please enter a repository URL.');
        return;
    }
    if (!repoUrl.startsWith('http')) {
        showError('Repository URL must start with http:// or https://');
        return;
    }

    const filesRaw = document.getElementById('files-input').value.trim();
    const files = filesRaw
        ? filesRaw.split('\n').map(f => f.trim()).filter(Boolean)
        : null;
    const maxFiles = parseInt(document.getElementById('max-files').value) || 3;
    const useMemory = document.getElementById('use-memory').checked;
    const useDefectApi = document.getElementById('use-defect-api').checked;

    dismissError();
    document.getElementById('submit-btn').disabled = true;
    document.getElementById('results-panel').classList.add('hidden');
    showStatus('Sending review request to agent...');

    try {
        const res = await fetch(`${API}/review/`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                repo_url: repoUrl,
                files: files,
                max_files: maxFiles,
                use_memory: useMemory,
                use_defect_api: useDefectApi,
            }),
            // Long timeout — agent may take several minutes for multi-file reviews
            signal: AbortSignal.timeout(600_000),
        });

        if (!res.ok) {
            let detail = 'Review failed';
            try { detail = (await res.json()).detail; } catch {}
            throw new Error(detail);
        }

        const session = await res.json();
        currentSession = session;
        hideStatus();
        renderResults(session);
        loadMemoryStats();

    } catch (e) {
        hideStatus();
        showError(e.message || 'Review request failed. Is the API running?');
    } finally {
        document.getElementById('submit-btn').disabled = false;
    }
}

// ── Render results ─────────────────────────────────────────────────────────────

function renderResults(session) {
    document.getElementById('results-panel').classList.remove('hidden');

    // Session header
    document.getElementById('session-id-label').textContent = `Session: ${session.session_id}`;
    document.getElementById('session-time-label').textContent =
        `${new Date(session.started_at).toLocaleString()} · ${session.total_elapsed_seconds.toFixed(1)}s`;

    // Summary cards
    const cards = document.getElementById('summary-cards');
    cards.innerHTML = [
        { n: session.files_reviewed,            label: 'Files Reviewed', cls: '' },
        { n: session.total_issues,              label: 'Total Issues',   cls: '' },
        { n: session.critical_issues,           label: 'Critical',       cls: session.critical_issues > 0 ? 'card-critical' : '' },
        { n: session.high_issues,               label: 'High',           cls: session.high_issues > 0    ? 'card-high'     : '' },
        { n: session.medium_issues,             label: 'Medium',         cls: '' },
        { n: session.low_issues,                label: 'Low',            cls: '' },
        { n: session.patterns_detected,         label: 'Patterns',       cls: session.patterns_detected > 0 ? 'card-pattern' : '' },
        { n: session.total_elapsed_seconds.toFixed(1) + 's', label: 'Total Time', cls: '' },
    ].map(c => `
        <div class="card ${c.cls}">
            <div class="card-number">${c.n}</div>
            <div class="card-label">${c.label}</div>
        </div>
    `).join('');

    // File tabs
    const tabs = document.getElementById('file-tabs');
    tabs.innerHTML = session.file_results.map((f, i) => {
        const name = baseName(f.file_path);
        const sev = getHighestSeverity(f.issues).toLowerCase();
        return `<button class="tab ${i === 0 ? 'active' : ''} tab-sev-${sev}"
                        onclick="showFile(${i})">${escapeHtml(name)}</button>`;
    }).join('');

    // Repo summary
    document.getElementById('repo-summary-text').textContent = session.repo_summary;

    if (session.file_results.length > 0) {
        showFile(0);
    }
}

function showFile(index) {
    const file = currentSession.file_results[index];

    // Update active tab
    document.querySelectorAll('.tab').forEach((t, i) =>
        t.classList.toggle('active', i === index));

    const issuesHtml = file.issues.length === 0
        ? '<p class="no-issues">&#10003; No issues found</p>'
        : file.issues.map(issue => `
            <div class="issue issue-${issue.severity.toLowerCase()}">
                <div class="issue-header">
                    <span class="severity-badge sev-${issue.severity.toLowerCase()}">${issue.severity}</span>
                    <span class="issue-title">${escapeHtml(issue.title)}</span>
                    ${issue.line_number != null ? `<span class="line-num">Line ${issue.line_number}</span>` : ''}
                    <span class="tool-badge">${issue.source_tool}</span>
                    <span class="cat-badge">${issue.category}</span>
                </div>
                <p class="issue-desc">${escapeHtml(issue.description)}</p>
                <div class="suggestion">
                    <strong>Suggestion:</strong> ${escapeHtml(issue.suggestion)}
                </div>
            </div>
        `).join('');

    const reviewHtml = file.final_review
        ? `<details class="agent-review">
               <summary>Agent Narrative Review</summary>
               <pre>${escapeHtml(file.final_review)}</pre>
           </details>`
        : '';

    const summaryHtml = file.summary
        ? `<div class="summary-box">${escapeHtml(file.summary)}</div>`
        : '';

    document.getElementById('file-content').innerHTML = `
        <div class="file-panel">
            <div class="file-header">
                <h3>${escapeHtml(baseName(file.file_path))}</h3>
                <span class="risk-score risk-${file.risk_label.toLowerCase()}">
                    ${file.risk_score.toFixed(3)} &bull; ${file.risk_label}
                </span>
                <span class="file-stats">
                    ${file.steps_taken} steps &bull; ${file.elapsed_seconds.toFixed(1)}s &bull; ${file.status}
                </span>
            </div>
            ${summaryHtml}
            <h4 class="section-title">Issues (${file.issues.length})</h4>
            ${issuesHtml}
            ${reviewHtml}
        </div>
    `;
}

// ── Memory stats ───────────────────────────────────────────────────────────────

async function loadMemoryStats() {
    const panel = document.getElementById('memory-stats');
    try {
        const res = await fetch(`${API}/memory/stats`);
        const data = await res.json();

        if (!data.connected) {
            panel.innerHTML = '<p class="warning">Neo4j not connected &mdash; running without memory</p>';
            return;
        }

        const rows = Object.entries(data.node_counts)
            .sort(([, a], [, b]) => b - a)
            .map(([label, count]) =>
                `<tr><td class="node-label">${label}</td><td class="node-count">${count}</td></tr>`)
            .join('');

        panel.innerHTML = `
            <table class="memory-table">
                <thead><tr><th>Node Type</th><th>Count</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
            <p class="memory-meta">
                ${data.total_relationships} relationships &bull;
                ${data.total_patterns} patterns detected &bull;
                ${data.total_reviews} reviews total
            </p>
        `;
    } catch {
        panel.innerHTML = '<p class="warning">Could not load memory stats</p>';
    }
}

// ── Status helpers ─────────────────────────────────────────────────────────────

function showStatus(msg) {
    document.getElementById('status-bar').classList.remove('hidden');
    document.getElementById('status-text').textContent = msg;
    statusStartTime = Date.now();
    clearInterval(statusTimerInterval);
    statusTimerInterval = setInterval(() => {
        const elapsed = ((Date.now() - statusStartTime) / 1000).toFixed(0);
        document.getElementById('status-timer').textContent = `${elapsed}s`;
    }, 1000);
}

function hideStatus() {
    clearInterval(statusTimerInterval);
    document.getElementById('status-bar').classList.add('hidden');
    document.getElementById('status-timer').textContent = '';
}

function showError(msg) {
    const banner = document.getElementById('error-banner');
    document.getElementById('error-text').textContent = msg;
    banner.classList.remove('hidden');
}

function dismissError() {
    document.getElementById('error-banner').classList.add('hidden');
}

// ── Utilities ──────────────────────────────────────────────────────────────────

function baseName(path) {
    return path.split(/[/\\]/).pop() || path;
}

function formatSeconds(s) {
    if (s < 60) return `${Math.round(s)}s`;
    const m = Math.floor(s / 60);
    const sec = Math.round(s % 60);
    return `${m}m ${sec}s`;
}

function getHighestSeverity(issues) {
    const order = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];
    for (const sev of order) {
        if (issues.some(i => i.severity === sev)) return sev;
    }
    return 'NONE';
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ── Init ───────────────────────────────────────────────────────────────────────
checkHealth();
setInterval(checkHealth, 30_000);