#!/usr/bin/env python3
"""Aipply Dashboard — live job application tracker with docx preview."""

import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from flask import Flask, abort, jsonify, render_template_string, request, send_file

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8092
TRACKER = ROOT / "output" / "tracker.json"

app = Flask(__name__)

STOP_FILE = ROOT / ".stop"

# Cycle state (thread-safe)
_cycle_lock = threading.Lock()
_cycle_state = {
    "running": False,
    "cycle_id": None,
    "total": 0,
    "processed": 0,
    "last_cycle": None,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_COLORS = {
    "applying": ("#6a1b9a", "#f3e5f5", "Applying"),
    "materials_ready": ("#1565c0", "#e3f2fd", "Materials Ready"),
    "applied": ("#2e7d32", "#e8f5e9", "Applied"),
    "rejected": ("#c62828", "#ffebee", "Rejected"),
    "interview": ("#f9a825", "#fff8e1", "Interview"),
    "manual_needed": ("#e65100", "#fff3e0", "Manual Needed"),
    "apply_failed": ("#c62828", "#ffebee", "Apply Failed"),
}


def _load_tracker():
    """Load tracker.json, return list of application dicts."""
    if not TRACKER.exists():
        return []
    with open(TRACKER) as f:
        data = json.load(f)
    return sorted(data, key=lambda x: x.get("applied_at", ""), reverse=True)


def _make_relative(abs_path: str) -> str:
    """Convert absolute path to relative from ROOT."""
    if not abs_path:
        return ""
    try:
        return str(Path(abs_path).relative_to(ROOT))
    except ValueError:
        return abs_path


def _format_date(iso_str: str) -> str:
    """Format ISO date string to 'Mar 24, 2026'."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return iso_str


def _load_settings():
    """Load config/settings.yaml from the project root."""
    try:
        with open(ROOT / "config" / "settings.yaml") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_profile():
    """Load config/profile.yaml from the project root."""
    try:
        with open(ROOT / "config" / "profile.yaml") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _score_color(score) -> str:
    """CSS color class for a CV score bucket."""
    if score is None:
        return ""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return ""
    if s >= 80:
        return "green"
    if s >= 60:
        return "gold"
    return "red"


def _is_today(iso_str: str) -> bool:
    try:
        return datetime.fromisoformat(iso_str).date() == datetime.now().date()
    except (ValueError, TypeError):
        return False


def _is_this_week(iso_str: str) -> bool:
    try:
        dt = datetime.fromisoformat(iso_str).date()
        return dt >= (datetime.now().date() - timedelta(days=7))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Aipply Dashboard</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #f0f2f5;
  color: #1a1a2e;
  min-height: 100vh;
}
/* Header */
.header {
  background: #1a1a2e;
  color: #fff;
  padding: 1.5rem 2rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header h1 { font-size: 1.6rem; font-weight: 700; letter-spacing: -.02em; }
.header .subtitle { opacity: .7; font-size: .85rem; }

/* Stats cards */
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem;
  padding: 1.5rem 2rem;
  max-width: 1400px;
  margin: 0 auto;
}
.stat-card {
  background: #fff;
  border-radius: 12px;
  padding: 1.25rem 1.5rem;
  box-shadow: 0 2px 8px rgba(0,0,0,.08);
}
.stat-card .label { font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; color: #666; margin-bottom: .35rem; }
.stat-card .value { font-size: 2rem; font-weight: 700; }
.stat-card .value.blue   { color: #1565c0; }
.stat-card .value.green  { color: #2e7d32; }
.stat-card .value.red    { color: #c62828; }
.stat-card .value.gold   { color: #f9a825; }

/* Table container */
.table-wrap {
  padding: 0 2rem 2rem;
  max-width: 1400px;
  margin: 0 auto;
}
.table-card {
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 2px 8px rgba(0,0,0,.08);
  overflow: hidden;
}
table { width: 100%; border-collapse: collapse; }
thead { background: #1a1a2e; color: #fff; }
th { padding: .85rem 1rem; text-align: left; font-weight: 600; font-size: .82rem; text-transform: uppercase; letter-spacing: .04em; }
td { padding: .75rem 1rem; border-bottom: 1px solid #eee; font-size: .9rem; vertical-align: middle; }
tr:hover td { background: #f8f9fa; }
tr:last-child td { border-bottom: none; }

/* CV score pill */
.score-pill { display: inline-block; min-width: 3.2rem; text-align: center; padding: .25rem .55rem; border-radius: 20px; font-weight: 700; font-size: .82rem; }
.score-pill.green { background: #e8f5e9; color: #2e7d32; }
.score-pill.gold { background: #fff8e1; color: #f9a825; }
.score-pill.red { background: #ffebee; color: #c62828; }
.score-pill.none { background: #eee; color: #999; }

/* AI optimize modal sections */
.score-breakdown { display: flex; gap: .75rem; flex-wrap: wrap; margin: .75rem 0; }
.score-breakdown .bd { background: #f5f7fa; border-radius: 8px; padding: .5rem .9rem; font-size: .85rem; }
.score-breakdown .bd b { display: block; font-size: 1.2rem; }
.changes-list li { margin-bottom: .35rem; }
.spinner { display: inline-block; width: 1rem; height: 1rem; border: 2px solid #ccc; border-top-color: #1565c0; border-radius: 50%; animation: spin .8s linear infinite; vertical-align: middle; }
@keyframes spin { to { transform: rotate(360deg); } }
.btn-ai { background: #6a1b9a !important; color: #fff !important; font-weight: 600; }
.btn-ai:hover { background: #4a148c !important; }

/* Status badge */
.badge {
  display: inline-block;
  padding: .25rem .75rem;
  border-radius: 20px;
  font-size: .78rem;
  font-weight: 600;
  white-space: nowrap;
}

/* Actions */
.actions { display: flex; gap: .5rem; flex-wrap: wrap; }
.actions a, .actions button {
  display: inline-flex; align-items: center; gap: .2rem;
  padding: .3rem .55rem;
  border-radius: 6px;
  font-size: .78rem;
  text-decoration: none;
  color: #1565c0;
  background: #e3f2fd;
  border: none;
  cursor: pointer;
  transition: background .15s;
}
.actions a:hover, .actions button:hover { background: #bbdefb; }
.actions .dl { color: #2e7d32; background: #e8f5e9; }
.actions .dl:hover { background: #c8e6c9; }
.actions .ext { color: #6a1b9a; background: #f3e5f5; }
.actions .ext:hover { background: #e1bee7; }
.actions .apply { color: #fff; background: #2e7d32; font-weight: 600; }
.actions .apply:hover { background: #1b5e20; }

/* Modal */
.modal-backdrop {
  display: none;
  position: fixed; inset: 0;
  background: rgba(0,0,0,.5);
  z-index: 1000;
  justify-content: center;
  align-items: center;
}
.modal-backdrop.open { display: flex; }
.modal {
  background: #fff;
  border-radius: 12px;
  max-width: 800px;
  width: 90%;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0,0,0,.25);
}
.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1rem 1.5rem;
  background: #1a1a2e;
  color: #fff;
  border-radius: 12px 12px 0 0;
  font-weight: 600;
}
.modal-header .close-btn {
  background: none; border: none; color: #fff; font-size: 1.5rem; cursor: pointer; line-height: 1;
}
.modal-body {
  padding: 1.5rem;
  overflow-y: auto;
  flex: 1;
  line-height: 1.7;
}
.modal-body h1, .modal-body h2, .modal-body h3 { margin: 1rem 0 .5rem; color: #1a1a2e; }
.modal-body p { margin-bottom: .75rem; }
.modal-body ul { margin: .5rem 0 .75rem 1.5rem; }
.modal-body li { margin-bottom: .25rem; }

/* Empty state */
.empty { text-align: center; padding: 3rem; color: #888; font-size: 1.1rem; }

/* Controls */
.controls { background: #fff; border-bottom: 1px solid #e0e0e0; padding: .75rem 2rem; }
.controls-inner { max-width: 1400px; margin: 0 auto; display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
.ctrl-btn { display: inline-flex; align-items: center; gap: .4rem; padding: .6rem 1.2rem; border: none; border-radius: 8px; font-size: .9rem; font-weight: 600; cursor: pointer; transition: all .15s; }
.ctrl-btn:disabled { opacity: .45; cursor: not-allowed; }
.ctrl-btn.green { background: #2e7d32; color: #fff; }
.ctrl-btn.green:hover:not(:disabled) { background: #1b5e20; }
.ctrl-btn.red { background: #c62828; color: #fff; }
.ctrl-btn.red:hover:not(:disabled) { background: #b71c1c; }
.ctrl-btn.resume { background: #1565c0; color: #fff; }
.ctrl-btn.resume:hover:not(:disabled) { background: #0d47a1; }
.status-indicator { margin-left: auto; font-weight: 600; font-size: .95rem; }

/* Responsive */
@media (max-width: 768px) {
  .header { padding: 1rem; }
  .stats { padding: 1rem; }
  .table-wrap { padding: 0 1rem 1rem; }
  .controls { padding: .75rem 1rem; }
  th, td { padding: .5rem; font-size: .8rem; }
}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>📋 Aipply Dashboard</h1>
    <div class="subtitle">Job Application Tracker</div>
  </div>
  <div class="subtitle">Last refresh: {{ now }}</div>
</div>

<!-- Controls -->
<div class="controls" id="controlsBar">
  <div class="controls-inner">
    <button class="ctrl-btn green" id="btnStartCycle" onclick="startCycle()">🚀 Start Cycle (5 jobs)</button>
    <button class="ctrl-btn red" id="btnStop" onclick="stopCycle()">🛑 Stop</button>
    <button class="ctrl-btn resume" id="btnResume" onclick="resumeOps()" style="display:none">▶️ Resume</button>
    <span class="status-indicator" id="statusIndicator">🟢 Idle</span>
  </div>
</div>

<div class="stats">
  <div class="stat-card">
    <div class="label">Total Applications</div>
    <div class="value">{{ total }}</div>
  </div>
  {% for status_key, info in status_counts.items() %}
  <div class="stat-card">
    <div class="label">{{ info.label }}</div>
    <div class="value {{ info.css }}">{{ info.count }}</div>
  </div>
  {% endfor %}
  <div class="stat-card">
    <div class="label">Today</div>
    <div class="value">{{ today_count }}</div>
  </div>
  <div class="stat-card">
    <div class="label">This Week</div>
    <div class="value">{{ week_count }}</div>
  </div>
</div>

<div class="table-wrap">
  <div class="table-card">
  {% if apps %}
    <table>
      <thead>
        <tr>
          <th>Company</th>
          <th>Position</th>
          <th>Location</th>
          <th>Posted</th>
          <th>CV Score</th>
          <th>Status</th>
          <th>Date Applied</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
      {% for a in apps %}
        <tr>
          <td><strong>{{ a.company }}</strong></td>
          <td>{{ a.position }}</td>
          <td>{{ a.location }}</td>
          <td title="{{ a.posted_at_fmt }}"><span class="badge" style="background:#e8eaf6;color:#283593">{{ a.posted_text or '—' }}</span></td>
          <td><span class="score-pill {{ a.score_css }}">{{ a.score_display }}</span></td>
          <td><span class="badge" style="background:{{ a.badge_bg }};color:{{ a.badge_fg }}">{{ a.badge_label }}</span></td>
          <td>{{ a.date_fmt }}</td>
          <td>
            <div class="actions">
              {% if a.can_apply %}
              <button class="apply" onclick="applyJob({{ a.id }})">🚀 Apply</button>
              {% endif %}
              {% if a.can_mark_applied %}
              <button class="apply" onclick="markApplied({{ a.id }})">✓ Mark applied</button>
              {% endif %}
              <button class="btn-ai" onclick="openCvStudio({{ a.id }})">🤖 CV</button>
              {% if a.resume_rel %}
              <button onclick="previewDoc('{{ a.resume_rel }}')">📄 Resume</button>
              <a class="dl" href="/download/{{ a.resume_rel }}">⬇️</a>
              {% endif %}
              {% if a.cover_letter_rel %}
              <button onclick="previewDoc('{{ a.cover_letter_rel }}')">✉️ Cover Letter</button>
              <a class="dl" href="/download/{{ a.cover_letter_rel }}">⬇️</a>
              {% endif %}
              {% if a.jd_rel %}
              <a href="/view/{{ a.jd_rel }}">📝 JD</a>
              {% endif %}
              {% if a.job_url %}
              <a class="ext" href="{{ a.job_url }}" target="_blank" rel="noopener">🔗 Job Post</a>
              {% endif %}
            </div>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  {% else %}
    <div class="empty">No applications tracked yet. Run the pipeline to get started!</div>
  {% endif %}
  </div>
</div>

<!-- Preview Modal -->
<div class="modal-backdrop" id="previewModal">
  <div class="modal">
    <div class="modal-header">
      <span id="modalTitle">Document Preview</span>
      <button class="close-btn" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modalBody">
      <p>Loading…</p>
    </div>
  </div>
</div>

<!-- AI CV Studio Modal -->
<div class="modal-backdrop" id="cvStudioModal">
  <div class="modal" style="max-width:900px">
    <div class="modal-header">
      <span id="cvStudioTitle">🤖 AI CV Studio</span>
      <button class="close-btn" onclick="closeCvStudio()">&times;</button>
    </div>
    <div class="modal-body" id="cvStudioBody">
      <p>Loading…</p>
    </div>
  </div>
</div>

<script>
function previewDoc(relPath) {
  const modal = document.getElementById('previewModal');
  const body  = document.getElementById('modalBody');
  const title = document.getElementById('modalTitle');

  body.innerHTML = '<p style="text-align:center;padding:2rem;color:#888">Loading preview…</p>';
  title.textContent = relPath.split('/').pop();
  modal.classList.add('open');

  fetch('/preview/' + relPath)
    .then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(data => {
      title.textContent = data.filename || relPath.split('/').pop();
      body.innerHTML = data.html;
    })
    .catch(err => {
      body.innerHTML = '<p style="color:#c62828;text-align:center">Failed to load preview: ' + err.message + '</p>';
    });
}

function closeModal() {
  document.getElementById('previewModal').classList.remove('open');
}

// Close on backdrop click
document.getElementById('previewModal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});

// Close on Escape
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeModal();
});

// --- Controls ---
function refreshStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(data => {
      const btnStart = document.getElementById('btnStartCycle');
      const btnStop  = document.getElementById('btnStop');
      const btnResume = document.getElementById('btnResume');
      const indicator = document.getElementById('statusIndicator');

      if (data.stopped) {
        btnStop.style.display = 'none';
        btnResume.style.display = '';
        btnStart.disabled = true;
        indicator.textContent = '🔴 Stopped';
      } else {
        btnStop.style.display = '';
        btnResume.style.display = 'none';
        indicator.textContent = '🟢 Idle';
      }

      if (data.cycle_running) {
        btnStart.disabled = true;
        indicator.textContent = '⏳ Cycle running (' + data.cycle_processed + '/' + data.cycle_total + ' jobs processed)';
      } else if (!data.stopped) {
        btnStart.disabled = false;
      }
    })
    .catch(() => {});
}

function startCycle() {
  if (!confirm('Start a new scan+apply cycle (5 jobs)?')) return;
  fetch('/api/start-cycle', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({limit: 5})
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { alert(data.error); return; }
      refreshStatus();
    })
    .catch(err => alert('Failed: ' + err));
}

function stopCycle() {
  fetch('/api/stop', { method: 'POST' })
    .then(() => refreshStatus())
    .catch(err => alert('Failed: ' + err));
}

function applyJob(id) {
  if (!confirm('Submit application #' + id + ' via LinkedIn Easy Apply?')) return;
  fetch('/api/apply', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id})
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { alert(data.error); return; }
      refreshStatus();
      waitForApplication(id);
    })
    .catch(err => alert('Failed: ' + err));
}

function waitForApplication(id) {
  const poll = () => fetch('/api/application/' + id)
    .then(r => r.json())
    .then(data => {
      if (data.error) throw new Error(data.error);
      if (data.status === 'applying') {
        setTimeout(poll, 2000);
      } else {
        location.reload();
      }
    })
    .catch(err => alert('Could not refresh application status: ' + err.message));
  poll();
}

function markApplied(id) {
  if (!confirm('Mark application #' + id + ' as applied? Use this after applying yourself on LinkedIn.')) return;
  fetch('/api/mark-applied', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id})
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { alert(data.error); return; }
      location.reload();
    })
    .catch(err => alert('Failed: ' + err));
}

function resumeOps() {
  fetch('/api/resume', { method: 'POST' })
    .then(() => refreshStatus())
    .catch(err => alert('Failed: ' + err));
}

// --- AI CV Studio ---
function openCvStudio(id) {
  const modal = document.getElementById('cvStudioModal');
  const body  = document.getElementById('cvStudioBody');
  modal.classList.add('open');
  body.innerHTML = '<p style="text-align:center;padding:2rem;color:#888"><span class="spinner"></span> Loading…</p>';
  fetch('/api/cv/status/' + id)
    .then(r => r.json())
    .then(data => {
      if (data.error) { body.innerHTML = '<p style="color:#c62828">' + data.error + '</p>'; return; }
      renderCvStudio(id, data);
    })
    .catch(err => { body.innerHTML = '<p style="color:#c62828">Failed: ' + err + '</p>'; });
}

function closeCvStudio() {
  document.getElementById('cvStudioModal').classList.remove('open');
}

document.getElementById('cvStudioModal').addEventListener('click', function(e) {
  if (e.target === this) closeCvStudio();
});

function renderCvStudio(id, data) {
  const body = document.getElementById('cvStudioBody');
  document.getElementById('cvStudioTitle').textContent =
    '🤖 AI CV Studio — ' + data.company + ' · ' + data.position;

  let html = '';

  // Score card
  if (data.score != null) {
    html += '<h3 style="margin-top:0">CV Score: <span class="score-pill ' + data.score_css + '">' + data.score + '/100</span>'
         + ' <small style="color:#888;font-weight:400">via ' + data.model + '</small></h3>';
    if (data.breakdown && Object.keys(data.breakdown).length) {
      html += '<div class="score-breakdown">';
      for (const [k, v] of Object.entries(data.breakdown)) {
        html += '<div class="bd"><b>' + v + '</b>' + k.replace('_', ' ') + '</div>';
      }
      html += '</div>';
    }
    if (data.recommendation) html += '<p><b>Recommendation:</b> ' + data.recommendation + '</p>';
    if (data.strengths && data.strengths.length) {
      html += '<p><b style="color:#2e7d32">Strengths:</b> ' + data.strengths.join(' · ') + '</p>';
    }
    if (data.gaps && data.gaps.length) {
      html += '<p><b style="color:#c62828">Gaps:</b> ' + data.gaps.join(' · ') + '</p>';
    }
  } else {
    html += '<p>Not scored yet.</p>';
  }

  if (data.analysis_available && data.requirements && data.requirements.length) {
    html += '<h3>Requirement evidence matrix' + (data.version ? ' · version ' + data.version : '') + '</h3><div style="overflow:auto"><table><thead><tr><th>Requirement</th><th>Priority</th><th>Status</th><th>Verified CV evidence</th></tr></thead><tbody>';
    data.requirements.forEach((r, i) => {
      html += '<tr data-req="' + i + '"><td contenteditable="true" class="req-text">' + r.requirement + '</td><td><select class="req-priority"><option' + (r.priority === 'critical' ? ' selected' : '') + '>critical</option><option' + (r.priority === 'required' ? ' selected' : '') + '>required</option><option' + (r.priority === 'preferred' ? ' selected' : '') + '>preferred</option></select></td><td>' + r.status + '</td><td>' + ((r.evidence || []).join(' · ') || '—') + '</td></tr>';
    });
    html += '</tbody></table></div><p><textarea id="cvRevisionInstruction" placeholder="Optional instruction, e.g. treat Kubernetes as preferred" style="width:100%;min-height:60px"></textarea></p><button class="btn-ai" onclick="reviseCvReview(' + id + ')">↻ Recalculate with edits</button>';
  } else if (data.fallback) {
    html += '<p style="background:#fff8e1;color:#7a5200;padding:.75rem;border-radius:6px"><b>Basic fallback:</b> AI requirement analysis is unavailable. This score is keyword overlap only.' + (data.ai_error ? ' Provider detail: ' + data.ai_error : '') + '</p>';
  }

  html += '<div class="actions" style="margin:1rem 0">'
       +   '<button class="btn-ai" onclick="cvScore(' + id + ')">⭐ Score CV</button>'
       +   '<button class="btn-ai" onclick="cvOptimize(' + id + ')">✨ Optimize CV for this job</button>'
       +   '<button onclick="cvReview(' + id + ')">🔍 Full review</button>'
       + '</div>';

  if (data.variant) {
    html += '<p style="color:#888;font-size:.85rem">CV variant: <b>' + data.variant + '</b>'
         + (data.updated_at ? ' · updated ' + data.updated_at : '') + '</p>';
  }

  html += '<div id="cvStudioResult"></div>';
  body.innerHTML = html;
}

function _cvBusy(msg) {
  document.getElementById('cvStudioResult').innerHTML =
    '<p style="text-align:center;padding:1rem"><span class="spinner"></span> ' + msg + ' (this can take up to a minute)…</p>';
}

function cvScore(id) {
  _cvBusy('Scoring CV against this job');
  fetch('/api/cv/score', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: id})
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { document.getElementById('cvStudioResult').innerHTML = '<p style="color:#c62828">' + data.error + '</p>'; return; }
      renderCvStudio(id, data);
    })
    .catch(err => alert('Failed: ' + err));
}

function cvOptimize(id) {
  _cvBusy('Optimizing CV for this job');
  fetch('/api/cv/optimize', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: id})
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { document.getElementById('cvStudioResult').innerHTML = '<p style="color:#c62828">' + data.error + '</p>'; return; }
      let html = '<h3>✨ Optimization proposal</h3>';
      if (data.fallback) {
        html += '<p style="background:#fff8e1;color:#7a5200;padding:.75rem;border-radius:6px">'
             + '<b>Offline fallback:</b> the AI provider returned no usable response. '
             + 'This proposal preserves existing resume wording and prioritizes relevant competencies.'
             + (data.ai_error ? ' Provider detail: ' + data.ai_error : '') + '</p>';
      }
      if (data.changes && data.changes.length) {
        html += '<p><b>What the AI changed:</b></p><ul class="changes-list">';
        data.changes.forEach(c => html += '<li>' + c + '</li>');
        html += '</ul>';
      }
      if (data.summary) html += '<p><b>New professional summary:</b></p><pre style="background:#f5f7fa;padding:1rem;border-radius:8px;white-space:pre-wrap">' + data.summary + '</pre>';
      if (data.competencies && data.competencies.length) {
        html += '<p><b>New competencies:</b></p><ul class="changes-list">';
        data.competencies.forEach(c => html += '<li>' + c + '</li>');
        html += '</ul>';
      }
      html += '<div class="actions" style="margin:1rem 0">'
           +   '<button class="apply" onclick="cvApplyUpdate(' + id + ')">✅ Review &amp; update CV</button>'
           +   '<button onclick="openCvStudio(' + id + ')">Cancel</button>'
           + '</div>'
           + '<div id="cvApplyResult"></div>';
      document.getElementById('cvStudioResult').innerHTML = html;
      window._cvProposal = data;
    })
    .catch(err => alert('Failed: ' + err));
}

function cvReview(id) {
  _cvBusy('Running full review');
  fetch('/api/cv/review', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: id})
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { document.getElementById('cvStudioResult').innerHTML = '<p style="color:#c62828">' + data.error + '</p>'; return; }
      renderCvStudio(id, data);
    })
    .catch(err => alert('Failed: ' + err));
}

function reviseCvReview(id) {
  const requirements = Array.from(document.querySelectorAll('#cvStudioBody tr[data-req]')).map(row => ({
    requirement: row.querySelector('.req-text').textContent.trim(), priority: row.querySelector('.req-priority').value, category: 'User edited'
  })).filter(item => item.requirement);
  const instruction = document.getElementById('cvRevisionInstruction').value;
  _cvBusy('Creating revised requirement analysis');
  fetch('/api/cv/review/revise', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: id, requirements: requirements, instruction: instruction})})
    .then(r => r.json()).then(data => {
      if (data.error) { document.getElementById('cvStudioResult').innerHTML = '<p style="color:#c62828">' + data.error + '</p>'; return; }
      renderCvStudio(id, data);
    }).catch(err => alert('Failed: ' + err));
}

function cvApplyUpdate(id) {
  const proposal = window._cvProposal;
  if (!proposal) { alert('No proposal to apply. Optimize first.'); return; }
  const res = document.getElementById('cvApplyResult');
  res.innerHTML = '<p><span class="spinner"></span> Writing optimized CV…</p>';
  fetch('/api/cv/update', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, summary: proposal.summary, competencies: proposal.competencies})
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { res.innerHTML = '<p style="color:#c62828">' + data.error + '</p>'; return; }
      res.innerHTML = '<p style="color:#2e7d32"><b>✅ CV updated:</b> ' + data.resume_file + '</p>'
                    + '<p><button onclick="previewDoc(\'' + data.resume_rel + '\')">📄 Preview new CV</button></p>';
    })
    .catch(err => alert('Failed: ' + err));
}

// Poll status every 10s
refreshStatus();
setInterval(refreshStatus, 10000);
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    apps_raw = _load_tracker()
    total = len(apps_raw)

    # Status breakdown
    css_map = {
        "materials_ready": "blue",
        "applied": "green",
        "rejected": "red",
        "interview": "gold",
    }
    status_counts = {}
    for key, (fg, bg, label) in STATUS_COLORS.items():
        count = sum(1 for a in apps_raw if a.get("status") == key)
        status_counts[key] = {"count": count, "label": label, "css": css_map.get(key, "")}

    today_count = sum(1 for a in apps_raw if _is_today(a.get("applied_at", "")))
    week_count = sum(1 for a in apps_raw if _is_this_week(a.get("applied_at", "")))

    # Prepare template data
    apps = []
    for a in apps_raw:
        status = a.get("status", "unknown")
        fg, bg, label = STATUS_COLORS.get(status, ("#666", "#eee", status.replace("_", " ").title()))
        score = a.get("cv_score")
        score_display = "—"
        score_css = "none"
        if score is not None:
            try:
                score_display = str(int(float(score)))
                score_css = _score_color(score)
            except (TypeError, ValueError):
                pass
        apps.append({
            "id": a.get("id"),
            "company": a.get("company", "—"),
            "position": a.get("position", "—"),
            "location": a.get("location", "—"),
            "posted_text": a.get("posted_text", ""),
            "posted_at_fmt": _format_date(a.get("posted_at", "")),
            "score_display": score_display,
            "score_css": score_css,
            "cv_variant": a.get("cv_variant", ""),
            "can_apply": status == "materials_ready" and bool(a.get("job_url")) and bool(a.get("resume_path")),
            "can_mark_applied": status not in {"applied", "applying"},
            "badge_fg": fg,
            "badge_bg": bg,
            "badge_label": label,
            "date_fmt": _format_date(a.get("applied_at", "")),
            "resume_rel": _make_relative(a.get("resume_path", "")),
            "cover_letter_rel": _make_relative(a.get("cover_letter_path", "")),
            "jd_rel": _make_relative(a.get("jd_file_path", "")),
            "job_url": a.get("job_url", ""),
        })

    now_str = datetime.now().strftime("%b %d, %Y at %I:%M %p")

    return render_template_string(
        DASHBOARD_HTML,
        apps=apps,
        total=total,
        status_counts=status_counts,
        today_count=today_count,
        week_count=week_count,
        now=now_str,
    )


@app.route("/download/<path:filepath>")
def download(filepath):
    full = ROOT / filepath
    if full.exists() and full.is_file():
        return send_file(str(full), as_attachment=True)
    abort(404)


@app.route("/view/<path:filepath>")
def view(filepath):
    full = ROOT / filepath
    if full.exists() and full.suffix == ".txt":
        text = full.read_text()
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{full.name}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       max-width: 800px; margin: 2rem auto; padding: 0 1.5rem; color: #1a1a2e; }}
a {{ color: #1565c0; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
pre {{ background: #f5f5f5; padding: 1.5rem; border-radius: 12px;
       white-space: pre-wrap; line-height: 1.7; font-size: .9rem;
       box-shadow: 0 2px 8px rgba(0,0,0,.08); }}
h2 {{ margin-bottom: 1rem; }}
</style></head>
<body>
<a href="/">← Back to Dashboard</a>
<h2>📝 {full.name}</h2>
<pre>{text}</pre>
</body></html>"""
    abort(404)


@app.route("/preview/<path:filepath>")
def preview(filepath):
    """Preview .docx files — extract formatted HTML via python-docx."""
    full = ROOT / filepath
    if not full.exists() or full.suffix != ".docx":
        abort(404)

    try:
        from docx import Document
    except ImportError:
        return jsonify({"error": "python-docx not installed"}), 500

    doc = Document(str(full))
    html_parts = []
    in_list = False

    for para in doc.paragraphs:
        style_name = para.style.name if para.style else ""

        # Build inline HTML from runs
        run_html = ""
        for run in para.runs:
            text = run.text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if not text:
                continue
            if run.bold and run.italic:
                text = f"<strong><em>{text}</em></strong>"
            elif run.bold:
                text = f"<strong>{text}</strong>"
            elif run.italic:
                text = f"<em>{text}</em>"
            run_html += text

        if not run_html.strip():
            # Close any open list before adding spacer
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            continue

        # Headings
        if style_name.startswith("Heading"):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            level = style_name.replace("Heading", "").strip()
            try:
                level = int(level)
            except ValueError:
                level = 2
            level = min(max(level, 1), 6)
            html_parts.append(f"<h{level}>{run_html}</h{level}>")

        # List items
        elif "List" in style_name or "Bullet" in style_name:
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{run_html}</li>")

        # Regular paragraph
        else:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<p>{run_html}</p>")

    if in_list:
        html_parts.append("</ul>")

    return jsonify({"html": "\n".join(html_parts), "filename": full.name})


# ---------------------------------------------------------------------------
# API Endpoints — Controls
# ---------------------------------------------------------------------------


@app.route("/api/stop", methods=["POST"])
def api_stop():
    STOP_FILE.write_text("stopped")
    return jsonify({"stopped": True})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    return jsonify({"stopped": False})


@app.route("/api/status")
def api_status():
    apps = _load_tracker()
    with _cycle_lock:
        cycle = dict(_cycle_state)
    return jsonify({
        "running": not STOP_FILE.exists(),
        "stopped": STOP_FILE.exists(),
        "total_applications": len(apps),
        "last_cycle": cycle.get("last_cycle"),
        "cycle_running": cycle["running"],
        "cycle_id": cycle["cycle_id"],
        "cycle_total": cycle["total"],
        "cycle_processed": cycle["processed"],
    })


# Per-application locks (an id being applied right now)
_applying: set[int] = set()
_applying_lock = threading.Lock()


@app.route("/api/apply", methods=["POST"])
def api_apply():
    """Submit a specific tracked application via LinkedIn Easy Apply.

    Body: {"id": <application id>} — must be in materials_ready state with a
    resume saved. Runs in a background thread; the row refreshes to applied
    or manual_needed when done.
    """
    if STOP_FILE.exists():
        return jsonify({"error": "Emergency stop is active (remove .stop first)"}), 409

    data = request.get_json(silent=True) or {}
    app_id = data.get("id")
    if not app_id:
        return jsonify({"error": "Missing id"}), 400

    from src.tracker import ApplicationTracker

    tracker = ApplicationTracker(tracker_path=str(TRACKER))
    record = next((a for a in tracker.applications if a.get("id") == app_id), None)
    if record is None:
        return jsonify({"error": f"Application #{app_id} not found"}), 404
    if record.get("status") != "materials_ready":
        return jsonify({"error": f"Application #{app_id} is '{record.get('status')}', not materials_ready"}), 409
    if not record.get("resume_path") or not Path(record["resume_path"]).exists():
        return jsonify({"error": f"Application #{app_id} has no saved resume"}), 409

    with _applying_lock:
        if app_id in _applying:
            return jsonify({"error": f"Application #{app_id} is already being applied"}), 409
        _applying.add(app_id)

    # Persist the in-progress state before starting the worker.  This gives
    # the browser something concrete to poll, rather than relying on a fixed
    # delay that can finish before LinkedIn has completed the submission.
    tracker.update_status(app_id, "applying", record.get("notes", ""))

    thread = threading.Thread(target=_apply_single_background, args=(app_id,), daemon=True)
    thread.start()
    return jsonify({"status": "started", "id": app_id})


def _apply_single_background(app_id: int):
    """Submit one application in a background thread and update the tracker."""
    from src.tracker import ApplicationTracker
    from src.linkedin_applicant import LinkedInApplicant

    try:
        with open(ROOT / "config" / "settings.yaml") as f:
            settings = yaml.safe_load(f) or {}
        with open(ROOT / "config" / "profile.yaml") as f:
            profile = yaml.safe_load(f) or {}

        tracker = ApplicationTracker(tracker_path=str(TRACKER))
        record = next((a for a in tracker.applications if a.get("id") == app_id), None)
        if record is None:
            return

        applicant = LinkedInApplicant(config=settings, profile=profile)
        applicant.connect_browser()
        try:
            result = applicant.apply_to_job(
                job={
                    "url": record.get("job_url", ""),
                    "title": record.get("position", ""),
                    "company": record.get("company", ""),
                },
                resume_path=record.get("resume_path", ""),
                cover_letter_path=record.get("cover_letter_path", "") or None,
            )
        finally:
            applicant.close()

        status = result.get("status", "apply_failed")
        new_status = "applied" if status == "applied" else "manual_needed"
        notes = (record.get("notes") or "") + f" | apply: {result.get('reason', status)}"

        # Copy any screenshots into the application folder
        out_dir = None
        if record.get("resume_path"):
            out_dir = Path(record["resume_path"]).parent
            for ss_path in result.get("screenshots", []):
                try:
                    if ss_path and Path(ss_path).exists():
                        import shutil
                        shutil.copy2(ss_path, out_dir / Path(ss_path).name)
                except Exception:
                    pass

        tracker.update_status(app_id, new_status, notes.strip())
        print(f"🚀 Apply #{app_id} {record.get('company')}: {new_status} ({result.get('reason', '')})")
    except Exception as e:
        try:
            ApplicationTracker(tracker_path=str(TRACKER)).update_status(
                app_id, "manual_needed", f"apply error: {e}"
            )
        except Exception:
            pass
        print(f"🚀 Apply #{app_id} failed: {e}")
    finally:
        with _applying_lock:
            _applying.discard(app_id)


@app.route("/api/start-cycle", methods=["POST"])
def api_start_cycle():
    """Kick off a scan+apply cycle in a background thread."""
    if STOP_FILE.exists():
        return jsonify({"error": "Emergency stop is active"}), 409

    with _cycle_lock:
        if _cycle_state["running"]:
            return jsonify({"error": "A cycle is already running", "cycle_id": _cycle_state["cycle_id"]}), 409

    data = request.get_json(silent=True) or {}
    limit = data.get("limit", 5)

    # Default keyword/location come from config/settings.yaml
    try:
        with open(ROOT / "config" / "settings.yaml") as f:
            _settings = yaml.safe_load(f) or {}
    except Exception:
        _settings = {}
    search_cfg = _settings.get("search", {})
    keyword = data.get("keyword") or (search_cfg.get("keywords") or ["devops engineer"])[0]
    location = data.get("location") or (search_cfg.get("locations") or ["Remote"])[0]

    cycle_id = str(uuid.uuid4())[:8]

    with _cycle_lock:
        _cycle_state.update({
            "running": True,
            "cycle_id": cycle_id,
            "total": 0,
            "processed": 0,
        })

    thread = threading.Thread(
        target=_run_cycle_background,
        args=(cycle_id, keyword, location, limit),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started", "cycle_id": cycle_id})


@app.route("/api/cycle-status")
def api_cycle_status():
    with _cycle_lock:
        return jsonify(dict(_cycle_state))


@app.route("/api/application/<int:app_id>")
def api_application(app_id):
    """Return the current persisted state for one dashboard row."""
    record = _get_record(app_id)
    if record is None:
        return jsonify({"error": f"Application #{app_id} not found"}), 404
    return jsonify({
        "id": app_id,
        "status": record.get("status", "unknown"),
        "updated_at": record.get("updated_at", ""),
    })


@app.route("/api/mark-applied", methods=["POST"])
def api_mark_applied():
    """Record an application completed manually on LinkedIn or another site."""
    data = request.get_json(silent=True) or {}
    app_id = data.get("id")
    if not isinstance(app_id, int):
        return jsonify({"error": "Missing or invalid id"}), 400

    from src.tracker import ApplicationTracker

    tracker = ApplicationTracker(tracker_path=str(TRACKER))
    record = next((a for a in tracker.applications if a.get("id") == app_id), None)
    if record is None:
        return jsonify({"error": f"Application #{app_id} not found"}), 404
    if record.get("status") == "applying":
        return jsonify({"error": f"Application #{app_id} is currently being submitted"}), 409

    note = (record.get("notes") or "").strip()
    manual_note = "manually marked applied from dashboard"
    if manual_note not in note:
        note = f"{note} | {manual_note}".strip(" |")
    updated = tracker.update_status(app_id, "applied", note)
    return jsonify({"id": app_id, "status": updated["status"]})


# ---------------------------------------------------------------------------
# API Endpoints — AI CV Studio (score / optimize / review / update)
# ---------------------------------------------------------------------------


def _get_record(app_id: int):
    """Fetch one tracker record or return None."""
    from src.tracker import ApplicationTracker

    tracker = ApplicationTracker(tracker_path=str(TRACKER))
    return next((a for a in tracker.applications if a.get("id") == app_id), None)


def _cv_settings_profile():
    return _load_settings(), _load_profile()


@app.route("/api/cv/status/<int:app_id>")
def api_cv_status(app_id):
    """Current CV score + details for one application."""
    record = _get_record(app_id)
    if record is None:
        return jsonify({"error": f"Application #{app_id} not found"}), 404

    details = record.get("cv_score_details") or {}
    return jsonify({
        "id": app_id,
        "company": record.get("company", ""),
        "position": record.get("position", ""),
        "score": record.get("cv_score"),
        "score_css": _score_color(record.get("cv_score")),
        "breakdown": details.get("breakdown", {}),
        "strengths": details.get("strengths", []),
        "gaps": details.get("gaps", []),
        "recommendation": details.get("recommendation", ""),
        "requirements": details.get("requirements", []),
        "analysis_available": details.get("analysis_available", False),
        "fallback": details.get("fallback", False),
        "ai_error": details.get("ai_error", ""),
        "version": details.get("version"),
        "history": [{"version": item.get("version"), "created_at": item.get("created_at")} for item in record.get("cv_review_history", [])],
        "model": details.get("model", ""),
        "variant": record.get("cv_variant", ""),
        "updated_at": record.get("updated_at", ""),
    })


def _run_cv_task(app_id: int, task: str, criteria=None, instruction="", persist_review=False):
    """Run score/review/optimize synchronously and return (result_dict, error)."""
    record = _get_record(app_id)
    if record is None:
        return None, (jsonify({"error": f"Application #{app_id} not found"}), 404)

    resume_path = record.get("resume_path", "")
    if not resume_path or not Path(resume_path).exists():
        return None, (jsonify({"error": "No saved resume for this application"}), 409)

    jd = record.get("job_description", "")
    jd_file = record.get("jd_file_path", "")
    if (not jd or len(jd) < 200) and jd_file and Path(jd_file).exists():
        try:
            jd = Path(jd_file).read_text()
        except Exception:
            pass
    if not jd:
        return None, (jsonify({"error": "No job description stored for this application"}), 409)

    settings, _profile = _cv_settings_profile()
    from src.cv_optimizer import CVOptimizer

    optimizer = CVOptimizer(config=settings)
    role = record.get("position", "")
    company = record.get("company", "")

    try:
        if task == "optimize":
            result = optimizer.optimize_resume(
                resume_path, jd, role=role, company=company,
                analysis=record.get("cv_score_details"),
            )
        else:
            result = optimizer.score_resume(resume_path, jd, role=role, company=company, criteria=criteria, instruction=instruction)
    except Exception as exc:
        return None, (jsonify({"error": f"CV task failed: {exc}"}), 500)

    if task != "optimize":
        # Persist score + details on the record
        from src.tracker import ApplicationTracker

        tracker = ApplicationTracker(tracker_path=str(TRACKER))
        if persist_review:
            result = tracker.append_cv_review(app_id, result) or result
        else:
            tracker.update_cv_data(app_id, cv_score=result.get("score"), score_details=result)

    return result, None


@app.route("/api/cv/score", methods=["POST"])
def api_cv_score():
    """Score the saved resume against the stored job description."""
    data = request.get_json(silent=True) or {}
    app_id = data.get("id")
    if not app_id:
        return jsonify({"error": "Missing id"}), 400

    result, error = _run_cv_task(app_id, "score")
    if error:
        return error

    record = _get_record(app_id) or {}
    return jsonify({
        "id": app_id,
        "company": record.get("company", ""),
        "position": record.get("position", ""),
        "score": result.get("score"),
        "score_css": _score_color(result.get("score")),
        "breakdown": result.get("breakdown", {}),
        "strengths": result.get("strengths", []),
        "gaps": result.get("gaps", []),
        "recommendation": result.get("recommendation", ""),
        "requirements": result.get("requirements", []),
        "analysis_available": result.get("analysis_available", False),
        "fallback": result.get("fallback", False),
        "ai_error": result.get("ai_error", ""),
        "version": result.get("version"),
        "model": result.get("model", ""),
        "variant": record.get("cv_variant", ""),
        "updated_at": record.get("updated_at", ""),
    })


@app.route("/api/cv/review", methods=["POST"])
def api_cv_review():
    """Create the first (or next) saved evidence-based review version."""
    data = request.get_json(silent=True) or {}
    app_id = data.get("id")
    if not app_id:
        return jsonify({"error": "Missing id"}), 400
    result, error = _run_cv_task(app_id, "review", persist_review=True)
    if error:
        return error
    record = _get_record(app_id) or {}
    return jsonify({
        "id": app_id, "company": record.get("company", ""), "position": record.get("position", ""),
        "score": result.get("score"), "score_css": _score_color(result.get("score")),
        "breakdown": result.get("breakdown", {}), "strengths": result.get("strengths", []),
        "gaps": result.get("gaps", []), "recommendation": result.get("recommendation", ""),
        "requirements": result.get("requirements", []), "analysis_available": result.get("analysis_available", False),
        "fallback": result.get("fallback", False), "ai_error": result.get("ai_error", ""), "model": result.get("model", ""), "version": result.get("version"),
        "history": [{"version": item.get("version"), "created_at": item.get("created_at")} for item in record.get("cv_review_history", [])],
    })


@app.route("/api/cv/review/revise", methods=["POST"])
def api_cv_review_revise():
    """Save a new review version from edited criteria and AI instructions."""
    data = request.get_json(silent=True) or {}
    app_id = data.get("id")
    criteria = data.get("requirements")
    instruction = str(data.get("instruction") or "")
    if not app_id:
        return jsonify({"error": "Missing id"}), 400
    if criteria is not None and not isinstance(criteria, list):
        return jsonify({"error": "Requirements must be a list"}), 400
    result, error = _run_cv_task(app_id, "review", criteria=criteria, instruction=instruction, persist_review=True)
    if error:
        return error
    record = _get_record(app_id) or {}
    return jsonify({
        "id": app_id, "company": record.get("company", ""), "position": record.get("position", ""),
        "score": result.get("score"), "score_css": _score_color(result.get("score")),
        "breakdown": result.get("breakdown", {}), "strengths": result.get("strengths", []),
        "gaps": result.get("gaps", []), "recommendation": result.get("recommendation", ""),
        "requirements": result.get("requirements", []), "analysis_available": result.get("analysis_available", False),
        "fallback": result.get("fallback", False), "ai_error": result.get("ai_error", ""), "model": result.get("model", ""), "version": result.get("version"),
        "history": [{"version": item.get("version"), "created_at": item.get("created_at")} for item in record.get("cv_review_history", [])],
    })


@app.route("/api/cv/optimize", methods=["POST"])
def api_cv_optimize():
    """Generate an optimization proposal (summary + competencies + changes).

    Nothing is written until /api/cv/update is called.
    """
    data = request.get_json(silent=True) or {}
    app_id = data.get("id")
    if not app_id:
        return jsonify({"error": "Missing id"}), 400

    result, error = _run_cv_task(app_id, "optimize")
    if error:
        return error

    if not result.get("summary") and not result.get("competencies"):
        return jsonify({
            "error": "AI optimization unavailable — set OPENROUTER_API_KEY in .env"
        }), 503

    return jsonify({
        "id": app_id,
        "summary": result.get("summary", ""),
        "competencies": result.get("competencies", []),
        "changes": result.get("changes", []),
        "model": result.get("model", ""),
        "fallback": bool(result.get("fallback")),
        "ai_error": result.get("ai_error", ""),
    })


@app.route("/api/cv/update", methods=["POST"])
def api_cv_update():
    """Write an approved optimization into a new version of the resume.

    Body: {"id": <app id>, "summary": str, "competencies": [str, ...]}
    Produces <name>_Resume_<role>_OPTIMIZED.docx next to the current resume
    and re-scores it, updating the tracker.
    """
    data = request.get_json(silent=True) or {}
    app_id = data.get("id")
    if not app_id:
        return jsonify({"error": "Missing id"}), 400
    summary = (data.get("summary") or "").strip()
    competencies = data.get("competencies") or []
    if not summary and not competencies:
        return jsonify({"error": "Nothing to apply — summary and competencies are empty"}), 400

    record = _get_record(app_id)
    if record is None:
        return jsonify({"error": f"Application #{app_id} not found"}), 404

    resume_path = record.get("resume_path", "")
    if not resume_path or not Path(resume_path).exists():
        return jsonify({"error": "No saved resume for this application"}), 409

    settings, _profile = _cv_settings_profile()
    from src.resume_tailor import ResumeTailor
    from src.tracker import ApplicationTracker

    src = Path(resume_path)
    dest = src.with_name(src.stem + "_OPTIMIZED.docx")
    try:
        ResumeTailor.clone_and_tailor(
            base_path=src,
            output_path=dest,
            new_summary=summary or None,
            new_competencies=competencies or None,
        )
    except Exception as exc:
        return jsonify({"error": f"Failed to write optimized CV: {exc}"}), 500

    # Re-score the optimized CV and persist
    jd = record.get("job_description", "")
    jd_file = record.get("jd_file_path", "")
    if (not jd or len(jd) < 200) and jd_file and Path(jd_file).exists():
        try:
            jd = Path(jd_file).read_text()
        except Exception:
            pass

    score_details = None
    if jd:
        try:
            from src.cv_optimizer import CVOptimizer

            optimizer = CVOptimizer(config=settings)
            score_result = optimizer.score_resume(str(dest), jd, role=record.get("position", ""), company=record.get("company", ""))
            score_details = score_result
        except Exception:
            score_details = None

    tracker = ApplicationTracker(tracker_path=str(TRACKER))
    tracker.update_cv_data(
        app_id,
        resume_path=str(dest),
        cv_score=score_details.get("score") if score_details else None,
        score_details=score_details,
    )

    return jsonify({
        "id": app_id,
        "resume_file": dest.name,
        "resume_rel": _make_relative(str(dest)),
        "new_score": score_details.get("score") if score_details else None,
    })


def _run_cycle_background(cycle_id, keyword, location, limit):
    """Run a scan+apply cycle in a background thread."""
    try:
        sys.path.insert(0, str(ROOT))
        from scripts.quick_run import scan_jobs, save_application

        with _cycle_lock:
            _cycle_state["total"] = 0
            _cycle_state["processed"] = 0

        print(f"🔍 Cycle {cycle_id}: Scanning LinkedIn...")
        jobs = scan_jobs(keyword=keyword, location=location, limit=limit)
        print(f"✅ Cycle {cycle_id}: Found {len(jobs)} jobs")

        with _cycle_lock:
            _cycle_state["total"] = len(jobs)

        for i, job in enumerate(jobs):
            if STOP_FILE.exists():
                print(f"🛑 Cycle {cycle_id} halted by stop after {i} jobs")
                break

            company = job.get("company", "Unknown")
            title = job.get("title", "Role").split("\n")[0]
            desc = job.get("description", "")
            print(f"📝 Cycle {cycle_id} [{i+1}/{len(jobs)}]: {company} — {title}")

            try:
                result = save_application(job=job, dry_run=False)
                if result:
                    print(f"  ✅ Applied: {company} — {title}")
                else:
                    print(f"  ⏭️ Skipped: {company} — {title}")
            except Exception as e:
                print(f"  ⚠️ Cycle {cycle_id}: Failed on job {i + 1}: {e}")

            with _cycle_lock:
                _cycle_state["processed"] = i + 1

    except Exception as e:
        print(f"❌ Cycle {cycle_id} failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        with _cycle_lock:
            _cycle_state["running"] = False
            _cycle_state["last_cycle"] = datetime.now().isoformat()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"🚀 Aipply Dashboard: http://localhost:{PORT}")
    app.run(port=PORT, debug=False)
