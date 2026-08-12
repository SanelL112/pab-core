import http.server
import socketserver
import json
import secrets
import glob
import os
import subprocess
import urllib.request
import urllib.parse
import re
import threading
from pathlib import Path

PORT = int(os.getenv("CLUSTER_MANAGER_PORT", "3000"))
CONFIG_PATH = os.getenv("CLUSTER_MANAGER_CONFIG_PATH", "/home/sanel-lathiya/llama-config.env")
MODELS_DIR = os.getenv("CLUSTER_MANAGER_MODELS_DIR", "/home/sanel-lathiya/models")
MAX_REQUEST_BYTES = 8 * 1024

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cluster Manager</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&family=Fira+Code&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0f172a; --glass-bg: rgba(30, 41, 59, 0.7);
            --glass-border: rgba(255, 255, 255, 0.1);
            --accent: #3b82f6; --accent-hover: #60a5fa;
            --text: #f8fafc; --text-muted: #94a3b8;
        }
        body {
            margin: 0; font-family: 'Inter', sans-serif; background: var(--bg);
            background-image: radial-gradient(at 0% 0%, rgba(59, 130, 246, 0.15) 0px, transparent 50%),
                              radial-gradient(at 100% 100%, rgba(139, 92, 246, 0.15) 0px, transparent 50%);
            color: var(--text); min-height: 100vh; display: flex; flex-direction: column; align-items: center; padding: 2rem; box-sizing: border-box;
        }
        .container { width: 100%; max-width: 900px; }
        .card {
            background: var(--glass-bg); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--glass-border); border-radius: 24px; padding: 2.5rem; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); margin-bottom: 2rem;
        }
        h1, h2 { margin: 0 0 1.5rem 0; font-weight: 600; background: linear-gradient(to right, #60a5fa, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        h1 { font-size: 2rem; } h2 { font-size: 1.5rem; }
        
        .status-badge {
            display: inline-flex; align-items: center; padding: 0.5rem 1rem; background: rgba(16, 185, 129, 0.1); color: #34d399;
            border-radius: 9999px; font-size: 0.875rem; font-weight: 600; margin-bottom: 1.5rem; border: 1px solid rgba(16, 185, 129, 0.2); transition: all 0.3s ease;
        }
        .status-badge.loading { background: rgba(245, 158, 11, 0.1); color: #fbbf24; border-color: rgba(245, 158, 11, 0.2); }
        .status-dot { width: 8px; height: 8px; background: #34d399; border-radius: 50%; margin-right: 8px; box-shadow: 0 0 8px #34d399; }
        .status-badge.loading .status-dot { background: #fbbf24; box-shadow: 0 0 8px #fbbf24; animation: pulse 1s infinite; }
        
        .search-bar { width: 100%; padding: 1rem 1.25rem; border-radius: 12px; border: 1px solid var(--glass-border); background: rgba(0,0,0,0.2); color: white; margin-bottom: 1.5rem; font-family: 'Inter', sans-serif; font-size: 1rem; box-sizing: border-box; outline: none; transition: border-color 0.2s; }
        .search-bar:focus { border-color: var(--accent); }
        
        .model-list { display: flex; flex-direction: column; gap: 1rem; }
        .model-item { display: flex; justify-content: space-between; align-items: center; padding: 1.25rem; background: rgba(255, 255, 255, 0.03); border: 1px solid var(--glass-border); border-radius: 16px; transition: all 0.2s ease; }
        .model-item:hover { background: rgba(255, 255, 255, 0.05); border-color: rgba(255, 255, 255, 0.2); }
        .model-item.active { border-color: var(--accent); background: rgba(59, 130, 246, 0.1); }
        .model-info { display: flex; flex-direction: column; gap: 0.25rem; }
        .model-name { font-weight: 600; font-size: 1.1rem; word-break: break-all; }
        .model-meta { font-size: 0.875rem; color: var(--text-muted); }
        
        button { background: var(--accent); color: white; border: none; padding: 0.75rem 1.5rem; border-radius: 12px; font-size: 0.875rem; font-weight: 600; cursor: pointer; transition: all 0.2s ease; box-shadow: 0 4px 14px 0 rgba(59, 130, 246, 0.39); min-width: 100px; }
        button:hover { background: var(--accent-hover); transform: translateY(-2px); box-shadow: 0 6px 20px rgba(59, 130, 246, 0.23); }
        button:disabled { background: #475569; cursor: not-allowed; transform: none; box-shadow: none; }
        
        .score-badge { display: inline-block; padding: 0.25rem 0.5rem; border-radius: 6px; font-size: 0.75rem; font-weight: 600; margin-top: 0.5rem; }
        .score-perfect { background: rgba(16, 185, 129, 0.2); color: #34d399; }
        .score-good { background: rgba(59, 130, 246, 0.2); color: #60a5fa; }
        .score-tight { background: rgba(245, 158, 11, 0.2); color: #fbbf24; }
        .score-bad { background: rgba(239, 68, 68, 0.2); color: #f87171; }

        .loading-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(8px); display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 50; opacity: 0; pointer-events: none; transition: opacity 0.3s ease; }
        .loading-overlay.active { opacity: 1; pointer-events: all; }
        
        .progress-container { width: 80%; max-width: 400px; background: rgba(255,255,255,0.1); border-radius: 999px; height: 12px; margin-top: 1.5rem; overflow: hidden; border: 1px solid rgba(255,255,255,0.05); }
        .progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #3b82f6, #8b5cf6); border-radius: 999px; transition: width 0.3s ease-out; box-shadow: 0 0 10px rgba(59, 130, 246, 0.5); }
        .progress-text { margin-top: 0.75rem; font-size: 0.875rem; color: var(--text-muted); font-weight: 600; }
        
        .dl-progress-container { width: 100%; background: rgba(255,255,255,0.1); border-radius: 999px; height: 8px; margin-top: 0.75rem; overflow: hidden; }
        .dl-progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #10b981, #3b82f6); border-radius: 999px; transition: width 0.5s linear; box-shadow: 0 0 8px rgba(16, 185, 129, 0.5); }
        
        .tabs { display: flex; gap: 1rem; margin-bottom: 2rem; }
        .tab { padding: 0.75rem 1.5rem; border-radius: 12px; cursor: pointer; font-weight: 600; background: rgba(255,255,255,0.05); color: var(--text-muted); transition: all 0.2s; }
        .tab.active { background: var(--accent); color: white; box-shadow: 0 4px 14px 0 rgba(59, 130, 246, 0.39); }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        
        .console-container {
            width: 100%; height: 500px; background: #000; border-radius: 12px; padding: 1rem;
            box-sizing: border-box; overflow-y: auto; border: 1px solid rgba(255,255,255,0.1);
            font-family: 'Fira Code', monospace; font-size: 0.85rem; color: #a3be8c; line-height: 1.4;
        }
        .console-line { word-break: break-all; margin-bottom: 2px; }
        
        .spinner-small { width: 20px; height: 20px; border: 2px solid rgba(255,255,255,0.1); border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; margin: 2rem auto; }
        
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="loading-overlay" id="loading">
        <h2 style="margin:0; font-weight: 400; font-size: 1.75rem;" id="loading-title">Switching Model...</h2>
        <p style="color: var(--text-muted); margin-top: 0.5rem; text-align: center;">Rebooting cluster RPC workers across all devices.</p>
        <div class="progress-container"><div class="progress-bar" id="progress-bar"></div></div>
        <div class="progress-text" id="progress-text">0%</div>
    </div>

    <div class="container">
        <div class="tabs">
            <div class="tab active" onclick="switchTab('local')">My Cluster Models</div>
            <div class="tab" onclick="switchTab('discover')">Discover & Download</div>
            <div class="tab" onclick="switchTab('console')">System Console</div>
            <div style="flex-grow: 1;"></div>
            <input type="password" id="api-token-input" class="search-bar" style="width:200px; margin-bottom: 0;" placeholder="API Token" onchange="sessionStorage.setItem('apiToken', this.value)">
        </div>
        
        <div class="card tab-content active" id="tab-local">
            <h1>Active Cluster Manager</h1>
            <div class="status-badge" id="main-status"><div class="status-dot"></div><span id="status-text">Cluster Ready</span></div>
            
            <div id="downloads-container" style="display:none; margin-bottom: 2rem;">
                <h3 style="margin-top:0; font-size:1.1rem;">Active Downloads</h3>
                <div class="model-list" id="downloads-list"></div>
            </div>

            <input type="text" class="search-bar" id="search-local" placeholder="Filter downloaded models..." onkeyup="filterLocalModels()">
            <div class="model-list" id="local-model-list"></div>
        </div>
        
        <div class="card tab-content" id="tab-discover">
            <h2>Discover Compatible Models</h2>
            <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1.5rem;">
                Search HuggingFace for single-file GGUF models. We automatically calculate a Compatibility Score based on your 18GB RAM cluster.
            </p>
            <div style="display:flex; gap:0.5rem; margin-bottom: 1.5rem;">
                <input type="text" class="search-bar" id="search-hf" style="margin:0;" placeholder="e.g. Llama 3 8B, Mistral, Qwen..." onkeypress="if(event.key === 'Enter') searchHF()">
                <button onclick="searchHF()">Search</button>
            </div>
            <div id="hf-results-container"><div class="model-list" id="hf-model-list"></div></div>
        </div>
        
        <div class="card tab-content" id="tab-console">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 1rem;">
                <h2 style="margin:0;">Llama.cpp Cluster Activity Log</h2>
                <div class="status-badge" style="margin:0; padding: 0.25rem 0.75rem;"><div class="status-dot" style="background:#3b82f6; box-shadow:0 0 8px #3b82f6; animation: pulse 2s infinite;"></div><span style="color:#60a5fa;">Live Tailing</span></div>
            </div>
            <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1.5rem;">
                Watch your cluster in real-time. See exactly what your RPC workers are doing, how fast inference is running, and live loading states.
            </p>
            <div class="console-container" id="console-output">
                <!-- Logs populated here -->
            </div>
        </div>
    </div>

    <script>
        let pollInterval = null;
        let consoleInterval = null;

        // Initialize token
        document.addEventListener('DOMContentLoaded', () => {
            if(sessionStorage.getItem('apiToken')) {
                const tokenInput = document.getElementById('api-token-input');
                if (tokenInput) tokenInput.value = sessionStorage.getItem('apiToken');
            }
        });

        async function apiFetch(url, options = {}) {
            options = options || {};
            options.headers = options.headers || {};
            const token = sessionStorage.getItem('apiToken');
            if(token) { options.headers['Authorization'] = 'Bearer ' + token; }
            return fetch(url, options);
        }

        function switchTab(tab) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById('tab-' + tab).classList.add('active');
            
            // Handle console polling
            if(tab === 'console') {
                fetchLogs();
                consoleInterval = setInterval(fetchLogs, 3000);
            } else if (consoleInterval) {
                clearInterval(consoleInterval);
                consoleInterval = null;
            }
        }

        async function fetchLogs() {
            try {
                const res = await apiFetch('/api/logs');
                const data = await res.json();
                const container = document.getElementById('console-output');
                
                // Only autoscroll if user is near the bottom
                const isNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50;
                
                container.innerHTML = '';
                const lines = data.logs.split('\\n');
                lines.forEach(line => {
                    if(line.trim() === '') return;
                    const el = document.createElement('div');
                    el.className = 'console-line';
                    
                    // Add some color coding for readability
                    if(line.includes('error') || line.includes('fail') || line.includes('E ')) {
                        el.style.color = '#bf616a';
                    } else if(line.includes('warn') || line.includes('W ')) {
                        el.style.color = '#ebcb8b';
                    } else if(line.includes('ms per token')) {
                        el.style.color = '#88c0d0';
                    } else if(line.includes('model loaded')) {
                        el.style.color = '#a3be8c';
                        el.style.fontWeight = 'bold';
                    }
                    
                    el.textContent = line;
                    container.appendChild(el);
                });
                
                if(isNearBottom) {
                    container.scrollTop = container.scrollHeight;
                }
            } catch (e) {}
        }

        function filterLocalModels() {
            const query = document.getElementById('search-local').value.toLowerCase();
            const items = document.querySelectorAll('#local-model-list .model-item');
            items.forEach(item => {
                const name = item.querySelector('.model-name').textContent.toLowerCase();
                item.style.display = name.includes(query) ? 'flex' : 'none';
            });
        }

        async function fetchModels() {
            try {
                const res = await apiFetch('/api/models');
                const data = await res.json();
                const list = document.getElementById('local-model-list');
                list.innerHTML = '';
                
                data.models.forEach(model => {
                    const isActive = model.name === data.active;
                    const sizeGB = (model.size / (1024*1024*1024)).toFixed(1);
                    const item = document.createElement('div');
                    item.className = `model-item ${isActive ? 'active' : ''}`;
                    item.innerHTML = `
                        <div class="model-info">
                            <div class="model-name">${model.name}</div>
                            <div class="model-meta">${sizeGB} GB • GGUF format</div>
                        </div>
                        <button ${isActive ? 'disabled' : ''} onclick="switchModel('${model.name}')">
                            ${isActive ? 'Active' : 'Load Model'}
                        </button>
                    `;
                    list.appendChild(item);
                });
                filterLocalModels();
            } catch (e) {}
        }

        async function checkDownloads() {
            try {
                const res = await apiFetch('/api/downloads');
                const data = await res.json();
                const container = document.getElementById('downloads-container');
                const list = document.getElementById('downloads-list');
                
                if (data.downloads.length > 0) {
                    container.style.display = 'block';
                    list.innerHTML = '';
                    data.downloads.forEach(dl => {
                        const item = document.createElement('div');
                        item.className = 'model-item';
                        item.style.flexDirection = 'column';
                        item.style.alignItems = 'stretch';
                        item.innerHTML = `
                            <div style="display:flex; justify-content:space-between;">
                                <div class="model-name" style="font-size:0.95rem;">Downloading: ${dl.filename}</div>
                                <div class="model-meta" style="color:#10b981; font-weight:600;">${dl.progress}%</div>
                            </div>
                            <div class="dl-progress-container">
                                <div class="dl-progress-bar" style="width: ${dl.progress}%"></div>
                            </div>
                        `;
                        list.appendChild(item);
                    });
                } else {
                    if (container.style.display !== 'none') {
                        container.style.display = 'none';
                        fetchModels(); // Refresh models since a download might have finished
                    }
                }
            } catch (e) {}
        }
        
        async function searchHF() {
            const query = document.getElementById('search-hf').value;
            if(!query) return;
            const list = document.getElementById('hf-model-list');
            list.innerHTML = '<div class="spinner-small"></div><div style="text-align:center;color:#94a3b8">Querying HuggingFace & verifying single-file compatibility...</div>';
            try {
                const res = await apiFetch(`/api/search?q=${encodeURIComponent(query)}`);
                const data = await res.json();
                list.innerHTML = '';
                if(data.results.length === 0) { list.innerHTML = '<div style="text-align:center;color:#94a3b8;padding:2rem;">No single-file compatible GGUFs found.</div>'; return; }
                
                data.results.forEach(model => {
                    const item = document.createElement('div');
                    item.className = 'model-item';
                    let scoreClass = model.score >= 85 ? 'score-perfect' : (model.score >= 70 ? 'score-good' : (model.score >= 50 ? 'score-tight' : 'score-bad'));
                    item.innerHTML = `
                        <div class="model-info">
                            <div class="model-name">${model.repo_id}</div>
                            <div class="model-meta" style="color: #cbd5e1; margin-top:0.2rem;">File: <b>${model.filename}</b></div>
                            <div class="model-meta">${model.size_gb} GB</div>
                            <div><span class="score-badge ${scoreClass}">Compatibility Score: ${model.score_text} (${model.score}/100)</span></div>
                        </div>
                        <button onclick="downloadModel('${model.url}', '${model.filename}', this)">Download</button>
                    `;
                    list.appendChild(item);
                });
            } catch(e) { list.innerHTML = `<div style="color:#f87171; text-align:center;">Error: ${e}</div>`; }
        }

        async function downloadModel(url, filename, btn) {
            btn.disabled = true; btn.textContent = "Starting...";
            try {
                const res = await apiFetch('/api/download', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ url, filename }) });
                if(res.ok) {
                    btn.textContent = "Downloading!"; btn.style.background = "#10b981";
                    switchTab('local');
                    checkDownloads();
                } else { btn.textContent = "Failed"; btn.disabled = false; }
            } catch(e) { btn.textContent = "Error"; btn.disabled = false; }
        }

        async function checkStatus() {
            try { const res = await apiFetch('/api/status'); const data = await res.json(); return data.status === 'ready'; } catch(e) { return false; }
        }

        async function switchModel(modelName) {
            document.getElementById('loading').classList.add('active');
            document.getElementById('loading-title').textContent = `Loading ${modelName}`;
            const pbar = document.getElementById('progress-bar');
            const ptext = document.getElementById('progress-text');
            pbar.style.width = '0%'; ptext.textContent = 'Initiating Cluster Reboot...';

            try {
                const res = await apiFetch('/api/switch', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ model: modelName }) });
                if(res.status === 422) {
                    let reason = 'Model rejected';
                    try { const d = await res.json(); reason = d.reason || reason; } catch(e) {}
                    alert('Cannot load this model:\\n\\n' + reason);
                    document.getElementById('loading').classList.remove('active');
                    return;
                }
                if(res.ok) {
                    let progress = 0;
                    
                    const logInterval = setInterval(async () => {
                        try {
                            const logRes = await apiFetch('/api/logs');
                            const logData = await logRes.json();
                            const lines = logData.logs.split('\\n').filter(l => l.trim() !== '');
                            if (lines.length > 0) {
                                let lastLine = lines[lines.length - 1];
                                // Try to extract the actual message, skipping the date/timestamp and module prefixes
                                let msg = lastLine;
                                if(msg.includes('srv')) msg = msg.split('srv')[1].trim();
                                else if(msg.includes('cmn')) msg = msg.split('cmn')[1].trim();
                                else if(msg.includes(']')) msg = msg.split(']')[1].trim();
                                
                                if (progress < 95) {
                                    progress += 0.8;
                                    pbar.style.width = `${progress}%`;
                                }
                                ptext.textContent = msg;
                            }
                        } catch(e) {}
                    }, 500);

                    pollInterval = setInterval(async () => {
                        if (await checkStatus()) {
                            clearInterval(pollInterval);
                            clearInterval(logInterval);
                            pbar.style.width = '100%'; ptext.textContent = 'Model Fully Loaded & RPC Connected!';
                            setTimeout(() => { document.getElementById('loading').classList.remove('active'); fetchModels(); }, 1500);
                        }
                    }, 2000);
                } else { alert("Failed to send switch request."); document.getElementById('loading').classList.remove('active'); }
            } catch(e) { alert("Error: " + e); document.getElementById('loading').classList.remove('active'); }
        }

        setInterval(async () => {
            const badge = document.getElementById('main-status');
            const text = document.getElementById('status-text');
            if (await checkStatus()) { badge.className = 'status-badge'; text.textContent = 'Cluster Ready'; } 
            else { badge.className = 'status-badge loading'; text.textContent = 'Cluster Loading...'; }
        }, 5000);

        setInterval(checkDownloads, 2000);
        
        fetchModels();
        checkDownloads();
    </script>
</body>
</html>
"""

def get_compatibility_score(size_gb):
    # Cluster combined usable RAM ~8-9GB (Surface 7.8G + Dell 5.8G + Pi 3.9G
    # physical, minus OS/rpc-worker overhead). Weights split across nodes via
    # RPC, so the file-size ceiling that actually loads is ~9GB (14B Q4_K_M).
    if size_gb < 6.0: return 100, "Perfect (Very Fast)"
    elif size_gb < 8.0: return 85, "Great (Optimal)"
    elif size_gb <= MAX_MODEL_GB: return 55, "Tight (Limit Context Window)"
    else: return 10, "Incompatible (Too Large — OOM)"

# ── Model validation (prevents crash-looping llama-web on bad models) ─────────
# The installed llama.cpp build supports ggml tensor type ids 0..MAX_GGML_TYPE.
# A model with a higher id (e.g. PQ2_0 = 142) fails to load and, because
# llama-web has Restart=always, crash-loops until systemd's rate limit trips —
# taking the whole cluster offline. We reject such models BEFORE writing config.
MAX_GGML_TYPE = 43
# Hard ceiling for model file size (GB). Above this, the model can't fit in the
# cluster's combined usable RAM and llama-server OOMs during load. Empirically
# 14B Q4_K_M (~8.4GB) loads across Surface+Dell+Pi; 24B/27B (14-16GB) OOM.
# Override with MAX_MODEL_GB env if you add RAM or more RPC workers.
MAX_MODEL_GB = float(os.getenv("MAX_MODEL_GB", "9.0"))

def _gguf_unsupported_quant(path):
    """Return the first unsupported ggml type id in a GGUF file, or None if OK.
    Header-only read — does not load weights, costs no RAM."""
    import struct
    def _rs(f):
        n = struct.unpack("<Q", f.read(8))[0]
        return f.read(n)
    def _skip_val(f, t):
        sizes = {0:1,1:1,2:2,3:2,4:4,5:4,6:4,7:1,10:8,11:8,12:8}
        if t in sizes: f.read(sizes[t]); return
        if t == 8: _rs(f); return
        if t == 9:
            et = struct.unpack("<I", f.read(4))[0]
            n = struct.unpack("<Q", f.read(8))[0]
            for _ in range(n): _skip_val(f, et)
            return
        raise ValueError(f"bad meta type {t}")
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF": return None  # not GGUF; let llama-server judge
            struct.unpack("<I", f.read(4))[0]              # version
            n_tensors = struct.unpack("<Q", f.read(8))[0]
            n_kv = struct.unpack("<Q", f.read(8))[0]
            for _ in range(n_kv):
                _rs(f)                                      # key
                vt = struct.unpack("<I", f.read(4))[0]
                _skip_val(f, vt)
            for _ in range(n_tensors):
                _rs(f)                                      # tensor name
                nd = struct.unpack("<I", f.read(4))[0]
                f.read(8 * nd)                              # dims
                tt = struct.unpack("<I", f.read(4))[0]
                f.read(8)                                   # offset
                if tt > MAX_GGML_TYPE:
                    return tt
    except Exception:
        return None  # unreadable header → don't block; llama-server will report
    return None

def validate_model(model_path):
    """(ok, reason). Blocks unloadable quants and oversized models so the
    cluster can't be knocked offline by a bad /api/switch."""
    if not os.path.exists(model_path):
        return False, "Model file not found"
    size_gb = os.path.getsize(model_path) / (1024 ** 3)
    if size_gb > MAX_MODEL_GB:
        return False, f"Too large ({size_gb:.1f}GB > {MAX_MODEL_GB}GB) — will OOM the cluster"
    bad = _gguf_unsupported_quant(model_path)
    if bad is not None:
        return False, f"Unsupported quant (ggml type {bad}); this llama.cpp build only supports 0-{MAX_GGML_TYPE}. Rebuild llama.cpp or use a Q4_K_M/Q2_0 file."
    return True, "OK"

class Handler(http.server.SimpleHTTPRequestHandler):
    def check_auth(self):
        token = os.environ.get('CLUSTER_MANAGER_API_TOKEN')
        if not token:
            return False
        auth_header = self.headers.get('Authorization')
        if not auth_header or not auth_header.startswith("Bearer "):
            return False
        provided_token = auth_header[7:]
        return secrets.compare_digest(provided_token, token)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", self.headers.get("content-length", "0")))
        except ValueError:
            return None
        if not 0 < length <= MAX_REQUEST_BYTES:
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))
            return

        if self.path.startswith('/api/'):
            if not self.check_auth():
                self.send_response(401)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return
            
        if self.path.startswith('/api/search?q='):
            query = urllib.parse.unquote(self.path.split('=')[1])
            results = []
            try:
                search_url = f"https://huggingface.co/api/models?search={urllib.parse.quote(query)}&filter=gguf&limit=5"
                req = urllib.request.Request(search_url)
                with urllib.request.urlopen(req, timeout=5) as res: repos = json.loads(res.read().decode())
                
                for repo in repos:
                    repo_id = repo['id']
                    tree_url = f"https://huggingface.co/api/models/{repo_id}/tree/main"
                    try:
                        treq = urllib.request.Request(tree_url)
                        with urllib.request.urlopen(treq, timeout=3) as tres:
                            files = json.loads(tres.read().decode())
                            valid_files = [f for f in files if f.get('path', '').endswith('.gguf') and '-of-' not in f.get('path', '') and 'split' not in f.get('path', '').lower()]
                            for vf in valid_files[:3]:
                                size_bytes = vf.get('size', 0)
                                if size_bytes == 0: continue
                                size_gb = round(size_bytes / (1024**3), 2)
                                score, text = get_compatibility_score(size_gb)
                                results.append({
                                    "repo_id": repo_id,
                                    "filename": vf['path'],
                                    "size_gb": size_gb,
                                    "score": score,
                                    "score_text": text,
                                    "url": f"https://huggingface.co/{repo_id}/resolve/main/{urllib.parse.quote(vf['path'])}"
                                })
                    except Exception: continue
            except Exception: pass
            
            results.sort(key=lambda x: x['score'], reverse=True)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"results": results}).encode('utf-8'))
            
        elif self.path == '/api/logs':
            try:
                output = subprocess.check_output(['sudo', 'journalctl', '-u', 'llama-web', '-n', '150', '--no-pager']).decode('utf-8', errors='replace')
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"logs": output}).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.end_headers()

        elif self.path == '/api/status':
            try:
                req = urllib.request.Request('http://127.0.0.1:8080/health')
                res = urllib.request.urlopen(req, timeout=1.5)
                if res.status == 200:
                    self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                    self.wfile.write(json.dumps({"status": "ready"}).encode('utf-8'))
                    return
            except Exception: pass
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({"status": "loading"}).encode('utf-8'))
            
        elif self.path == '/api/downloads':
            downloads = []
            for path in glob.glob(os.path.join(MODELS_DIR, 'wget_*.log')):
                filename = os.path.basename(path)[5:-4]
                if filename == '14b': filename = 'Qwen2.5-14B-Instruct-Q4_K_M.gguf'
                elif filename == '24b': filename = 'Mistral-Small-24B-Instruct-2501-Q4_K_M.gguf'
                elif filename == '9b_qwythos': filename = 'Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M.gguf'
                elif filename == 'qwen35': filename = 'Qwen3.5-9B-Q4_K_M.gguf'
                
                progress = 0
                try:
                    with open(path, 'r') as f:
                        lines = f.readlines()[-15:]
                        for line in reversed(lines):
                            match = re.search(r'(\d+)%', line)
                            if match:
                                progress = int(match.group(1))
                                break
                except Exception: pass
                downloads.append({"filename": filename, "progress": progress})
            
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({"downloads": downloads}).encode('utf-8'))

        elif self.path == '/api/models':
            active = ""
            try:
                with open(CONFIG_PATH, 'r') as f:
                    for line in f:
                        if line.startswith('MODEL_PATH='):
                            active = os.path.basename(line.split('=')[1].strip().strip('"'))
            except Exception: pass
            
            downloading = set()
            for log_path in glob.glob(os.path.join(MODELS_DIR, 'wget_*.log')):
                fname = os.path.basename(log_path)[5:-4]
                if fname == '14b': fname = 'Qwen2.5-14B-Instruct-Q4_K_M.gguf'
                elif fname == '24b': fname = 'Mistral-Small-24B-Instruct-2501-Q4_K_M.gguf'
                elif fname == '9b_qwythos': fname = 'Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M.gguf'
                elif fname == 'qwen35': fname = 'Qwen3.5-9B-Q4_K_M.gguf'
                downloading.add(fname)

            models = []
            for path in glob.glob(os.path.join(MODELS_DIR, '*.gguf')):
                name = os.path.basename(path)
                if "-of-" not in name and name not in downloading:
                    models.append({"name": name, "path": path, "size": os.path.getsize(path)})
            models.sort(key=lambda x: x['name'].lower())
            
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({"active": active, "models": models}).encode('utf-8'))
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path.startswith('/api/'):
            if not self.check_auth():
                self.send_response(401)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

        if self.path == '/api/switch':
            body = self.read_json_body()
            if body is None:
                self.send_response(400); self.end_headers(); return
            model_name = body.get('model')
            if not isinstance(model_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.gguf", model_name):
                self.send_response(400); self.end_headers(); return
                
            model_path = os.path.join(MODELS_DIR, model_name)
            if not os.path.exists(model_path):
                self.send_response(404); self.end_headers(); return

            # Validate BEFORE touching config: an unloadable quant or an
            # oversized model would crash-loop llama-web and take the cluster
            # down. Reject with a clear reason and leave the running model alone.
            ok, reason = validate_model(model_path)
            if not ok:
                self.send_response(422)
                self.send_header('Content-type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({"status": "rejected", "reason": reason}).encode('utf-8'))
                return

            with open(CONFIG_PATH, 'r') as f: lines = f.readlines()
            with open(CONFIG_PATH, 'w') as f:
                for line in lines:
                    if line.startswith('MODEL_PATH='): f.write(f'MODEL_PATH="{model_path}"\n')
                    else: f.write(line)
            subprocess.Popen(['sudo', 'systemctl', 'restart', 'llama-web'])
            self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
                
        elif self.path == '/api/download':
            body = self.read_json_body()
            if body is None:
                self.send_response(400); self.end_headers(); return
            url = body.get('url')
            filename = body.get('filename')
            
            if isinstance(url, str) and filename:
                if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.gguf", filename):
                    self.send_response(400); self.end_headers(); return

                if not url.startswith('https://huggingface.co/') or '/resolve/' not in url:
                    self.send_response(400); self.end_headers(); return

                out_path = os.path.join(MODELS_DIR, filename)
                log_path = os.path.join(MODELS_DIR, f"wget_{filename}.log")

                def download() -> None:
                    try:
                        with open(log_path, "wb") as log_file:
                            result = subprocess.run(
                                ["wget", "-c", url, "-O", out_path],
                                stdout=log_file,
                                stderr=subprocess.STDOUT,
                                check=False,
                                timeout=8 * 60 * 60,
                            )
                        if result.returncode == 0:
                            Path(log_path).unlink(missing_ok=True)
                    except (OSError, subprocess.SubprocessError):
                        pass

                threading.Thread(target=download, daemon=True, name="cluster-model-download").start()
                self.send_response(200); self.send_header('Content-type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({"status": "started"}).encode('utf-8'))
            else:
                self.send_response(400); self.end_headers()

class ThreadingSimpleServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    pass

if __name__ == '__main__':
    bind_host = os.environ.get('CLUSTER_MANAGER_BIND_HOST', '127.0.0.1')
    if bind_host not in {'127.0.0.1', '::1', 'localhost'} and not os.environ.get('CLUSTER_MANAGER_API_TOKEN'):
        raise SystemExit('CLUSTER_MANAGER_API_TOKEN is required outside loopback')
    with ThreadingSimpleServer((bind_host, PORT), Handler) as httpd:
        print("Serving at", bind_host, "port", PORT)
        httpd.serve_forever()
