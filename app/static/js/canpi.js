/*
 * CanPi Dashboard JavaScript
 * Real-time CAN frame streaming, decoded gauges, charts, and capture controls.
 */
document.addEventListener('DOMContentLoaded', () => {
    // ── DOM Elements ────────────────────────────────────────────────
    const consoleEl = document.getElementById('console');
    const startBtn = document.getElementById('start-capture');
    const stopBtn = document.getElementById('stop-capture');
    const exportBtn = document.getElementById('export-btn');
    const usbLogBtn = document.getElementById('usb-log-btn');
    const clearBtn = document.getElementById('clear-console');
    const canSelect = document.getElementById('can-select');
    const statusBadge = document.getElementById('capture-status');
    const frameCountEl = document.getElementById('frame-count');
    const consoleCountEl = document.getElementById('console-count');
    const gaugesContainer = document.getElementById('gauges-container');
    const busStatsEl = document.getElementById('bus-stats');
    const idFreqEl = document.getElementById('id-freq-table');

    // PAMAS controls (admin only)
    const pamasStart = document.getElementById('pamas-start');
    const pamasStop = document.getElementById('pamas-stop');
    const pamasData = document.getElementById('pamas-data');
    const pamasIdle = document.getElementById('pamas-idle');

    if (!startBtn) return;

    // ── State ───────────────────────────────────────────────────────
    let stream = null;
    let frameCount = 0;
    let consoleLines = 0;
    let capturing = false;
    const MAX_CONSOLE_LINES = 500;
    const MAX_CHART_POINTS = 60;

    // Frame rate tracking
    let rateWindow = [];
    const idCounts = {};

    // Decoded signal values received via SSE
    const decodedValues = {};

    // Category display order and icons
    const CATEGORIES = {
        engine: { label: 'Engine', icon: 'bi-gear-fill' },
        vehicle: { label: 'Vehicle', icon: 'bi-truck' },
        transmission: { label: 'Transmission', icon: 'bi-sliders' },
        electrical: { label: 'Electrical', icon: 'bi-lightning-charge' },
        environment: { label: 'Environment', icon: 'bi-thermometer-half' },
        diagnostics: { label: 'Diagnostics', icon: 'bi-exclamation-triangle' },
    };

    // ── Charts (initialized lazily when tab shown) ──────────────────
    let rateChart = null, idChart = null, rpmChart = null, speedChart = null;
    const COLORS = ['#00d4aa','#ff6384','#36a2eb','#ffce56','#9966ff','#ff9f40','#4bc0c0','#e7e9ed'];

    function ensureCharts() {
        if (rateChart) return;
        const rateCtx = document.getElementById('rate-chart');
        const idCtx = document.getElementById('id-chart');
        if (!rateCtx || !idCtx) return;

        const chartDefaults = {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 0 },
            scales: {
                x: { display: true, grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { maxTicksLimit: 10 } },
                y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.05)' } }
            },
            plugins: { legend: { display: false } }
        };

        rateChart = new Chart(rateCtx, {
            type: 'line',
            data: { labels: [], datasets: [{ label: 'Frames/sec', data: [], borderColor: '#00d4aa',
                backgroundColor: 'rgba(0,212,170,0.1)', fill: true, tension: 0.3, pointRadius: 0 }] },
            options: chartDefaults,
        });

        idChart = new Chart(idCtx, {
            type: 'doughnut',
            data: { labels: [], datasets: [{ data: [], backgroundColor: COLORS }] },
            options: { responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
                plugins: { legend: { position: 'right', labels: { boxWidth: 12, font: { size: 11 } } } } },
        });

        const rpmCtx = document.getElementById('rpm-trend-chart');
        const speedCtx = document.getElementById('speed-trend-chart');
        if (rpmCtx) {
            rpmChart = new Chart(rpmCtx, {
                type: 'line',
                data: { labels: [], datasets: [{ label: 'RPM', data: [], borderColor: '#ff6384',
                    backgroundColor: 'rgba(255,99,132,0.1)', fill: true, tension: 0.3, pointRadius: 0 }] },
                options: { ...chartDefaults,
                    scales: { ...chartDefaults.scales, y: { ...chartDefaults.scales.y, suggestedMax: 6000 } } },
            });
        }
        if (speedCtx) {
            speedChart = new Chart(speedCtx, {
                type: 'line',
                data: { labels: [], datasets: [{ label: 'km/h', data: [], borderColor: '#36a2eb',
                    backgroundColor: 'rgba(54,162,235,0.1)', fill: true, tension: 0.3, pointRadius: 0 }] },
                options: { ...chartDefaults,
                    scales: { ...chartDefaults.scales, y: { ...chartDefaults.scales.y, suggestedMax: 200 } } },
            });
        }
    }

    // Initialize charts when the Charts tab is first shown
    document.querySelectorAll('[data-bs-target="#tab-charts"]').forEach(el => {
        el.addEventListener('shown.bs.tab', () => ensureCharts());
    });

    // ── Gauge Rendering ─────────────────────────────────────────────
    function buildGaugeHTML(key, sig) {
        const pct = sig.max > sig.min ? ((sig.value - sig.min) / (sig.max - sig.min)) * 100 : 50;
        const clampPct = Math.max(0, Math.min(100, pct));
        const barColor = sig.status === 'danger' ? '#dc3545' : sig.status === 'warning' ? '#ffc107' : '#00d4aa';
        const formatted = sig.formatted || String(sig.value);
        return `
            <div class="col-6 col-sm-4 col-md-3 col-lg-2 mb-2" id="gauge-${key}">
                <div class="card gauge-card ${sig.status !== 'normal' ? 'status-' + sig.status : ''}">
                    <div class="card-body p-2 text-center">
                        <div class="gauge-name"><i class="bi ${sig.icon || 'bi-speedometer'}"></i> ${sig.name}</div>
                        <div class="gauge-value ${sig.status}">${formatted}</div>
                        <div class="gauge-unit">${sig.unit}</div>
                        <div class="gauge-bar"><div class="gauge-bar-fill" style="width:${clampPct}%;background:${barColor};"></div></div>
                    </div>
                </div>
            </div>`;
    }

    function renderGauges() {
        if (!gaugesContainer || Object.keys(decodedValues).length === 0) return;

        // Group by category
        const groups = {};
        for (const [key, sig] of Object.entries(decodedValues)) {
            const cat = sig.category || 'other';
            if (!groups[cat]) groups[cat] = {};
            groups[cat][key] = sig;
        }

        let html = '';
        for (const [catKey, catInfo] of Object.entries(CATEGORIES)) {
            if (!groups[catKey]) continue;
            html += `<div class="category-header mb-2"><i class="bi ${catInfo.icon}"></i> ${catInfo.label}</div>`;
            html += '<div class="row">';
            for (const [key, sig] of Object.entries(groups[catKey])) {
                html += buildGaugeHTML(key, sig);
            }
            html += '</div>';
        }
        gaugesContainer.innerHTML = html;
    }

    function updateSingleGauge(key, sig) {
        const el = document.getElementById('gauge-' + key);
        if (!el) { renderGauges(); return; }
        const card = el.querySelector('.gauge-card');
        const valueEl = el.querySelector('.gauge-value');
        const barFill = el.querySelector('.gauge-bar-fill');
        if (!valueEl) return;

        valueEl.textContent = sig.formatted || String(sig.value);
        valueEl.className = 'gauge-value ' + sig.status;
        card.className = 'card gauge-card' + (sig.status !== 'normal' ? ' status-' + sig.status : '');

        const pct = sig.max > sig.min ? ((sig.value - sig.min) / (sig.max - sig.min)) * 100 : 50;
        const clampPct = Math.max(0, Math.min(100, pct));
        const barColor = sig.status === 'danger' ? '#dc3545' : sig.status === 'warning' ? '#ffc107' : '#00d4aa';
        barFill.style.width = clampPct + '%';
        barFill.style.background = barColor;
    }

    // ── Rate & chart update (every second) ──────────────────────────
    setInterval(() => {
        const now = Date.now() / 1000;
        rateWindow = rateWindow.filter(t => t > now - 1);
        const rate = rateWindow.length;
        const label = new Date().toLocaleTimeString();

        if (rateChart) {
            rateChart.data.labels.push(label);
            rateChart.data.datasets[0].data.push(rate);
            if (rateChart.data.labels.length > MAX_CHART_POINTS) {
                rateChart.data.labels.shift();
                rateChart.data.datasets[0].data.shift();
            }
            rateChart.update();
        }

        if (idChart) {
            const sorted = Object.entries(idCounts).sort((a, b) => b[1] - a[1]).slice(0, 8);
            idChart.data.labels = sorted.map(e => '0x' + parseInt(e[0]).toString(16).toUpperCase());
            idChart.data.datasets[0].data = sorted.map(e => e[1]);
            idChart.update();
        }

        // Push decoded RPM & speed to trend charts
        if (rpmChart && decodedValues.rpm) {
            rpmChart.data.labels.push(label);
            rpmChart.data.datasets[0].data.push(decodedValues.rpm.value);
            if (rpmChart.data.labels.length > MAX_CHART_POINTS) { rpmChart.data.labels.shift(); rpmChart.data.datasets[0].data.shift(); }
            rpmChart.update();
        }
        if (speedChart && decodedValues.speed) {
            speedChart.data.labels.push(label);
            speedChart.data.datasets[0].data.push(decodedValues.speed.value);
            if (speedChart.data.labels.length > MAX_CHART_POINTS) { speedChart.data.labels.shift(); speedChart.data.datasets[0].data.shift(); }
            speedChart.update();
        }

        // Refresh bus stats & ID table when bus tab is visible
        if (capturing) {
            refreshBusStats();
        }
    }, 1000);

    // ── Bus stats polling ───────────────────────────────────────────
    async function refreshBusStats() {
        try {
            const resp = await fetch('/can/stats');
            if (!resp.ok) return;
            const s = resp.ok ? await resp.json() : null;
            if (!s || !busStatsEl) return;
            busStatsEl.innerHTML = `
                <div class="bus-stat mb-1">Total Frames: <span class="value">${s.total_frames.toLocaleString()}</span></div>
                <div class="bus-stat mb-1">Frame Rate: <span class="value">${(s.frames_per_sec || 0).toFixed(1)} fps</span></div>
                <div class="bus-stat mb-1">Decode Errors: <span class="value">${s.decode_errors || 0}</span></div>
                <div class="bus-stat mb-1">Unique IDs: <span class="value">${s.unique_ids}</span></div>
                <div class="bus-stat mb-1">Uptime: <span class="value">${(s.uptime_sec || 0).toFixed(0)}s</span></div>`;

            if (idFreqEl && s.id_frequencies) {
                const rows = Object.entries(s.id_frequencies).sort((a,b) => b[1] - a[1]).map(([id, cnt]) =>
                    `<tr><td class="ps-3"><code>${id}</code></td><td>${cnt.toLocaleString()}</td></tr>`
                ).join('');
                idFreqEl.innerHTML = `<table class="table table-sm table-dark mb-0"><thead><tr><th class="ps-3">ID</th><th>Count</th></tr></thead><tbody>${rows}</tbody></table>`;
            }
        } catch (e) { /* silent */ }
    }

    // ── Helpers ─────────────────────────────────────────────────────
    function setCapturing(active) {
        capturing = active;
        startBtn.disabled = active;
        stopBtn.disabled = !active;
        exportBtn.disabled = !active;
        usbLogBtn.disabled = !active;
        statusBadge.textContent = active ? 'Capturing' : 'Idle';
        statusBadge.className = 'badge ' + (active ? 'bg-success' : 'bg-secondary');
    }

    function appendConsole(text) {
        if (!consoleEl) return;
        consoleEl.textContent += text + '\n';
        consoleLines++;
        if (consoleLines > MAX_CONSOLE_LINES) {
            const lines = consoleEl.textContent.split('\n');
            consoleEl.textContent = lines.slice(lines.length - MAX_CONSOLE_LINES).join('\n');
            consoleLines = MAX_CONSOLE_LINES;
        }
        consoleEl.scrollTop = consoleEl.scrollHeight;
        if (consoleCountEl) consoleCountEl.textContent = consoleLines + ' lines';
    }

    // ── Start / Stop ────────────────────────────────────────────────
    startBtn.addEventListener('click', async () => {
        const selected = canSelect.value;
        const interfaces = selected.split(',').map(s => s.trim());
        try {
            const resp = await fetch('/can/start_capture', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ interfaces })
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                alert(err.description || 'Failed to start capture');
                return;
            }
            setCapturing(true);
            startStream();
        } catch (e) {
            alert('Connection error: ' + e.message);
        }
    });

    stopBtn.addEventListener('click', async () => {
        try {
            await fetch('/can/stop_capture', { method: 'POST' });
        } catch (e) { /* ignore */ }
        stopStream();
        setCapturing(false);
    });

    exportBtn.addEventListener('click', async () => {
        try {
            const resp = await fetch('/capture/export');
            if (resp.ok) {
                const blob = await resp.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'can_capture.csv';
                a.click();
                URL.revokeObjectURL(url);
            }
        } catch (e) {
            alert('Export failed: ' + e.message);
        }
    });

    usbLogBtn.addEventListener('click', async () => {
        try {
            const resp = await fetch('/usb/log', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });
            const data = await resp.json();
            if (resp.ok) {
                alert('Logged ' + data.frames + ' frames to ' + data.file);
            } else {
                alert(data.description || 'USB log failed');
            }
        } catch (e) {
            alert('USB log error: ' + e.message);
        }
    });

    clearBtn.addEventListener('click', () => {
        if (consoleEl) consoleEl.textContent = '';
        consoleLines = 0;
        if (consoleCountEl) consoleCountEl.textContent = '0 lines';
    });

    // ── SSE Stream ──────────────────────────────────────────────────
    let gaugeRenderQueued = false;

    function startStream() {
        if (stream) stream.close();

        const eventSource = new EventSource('/can/stream');
        stream = eventSource;

        eventSource.onmessage = (event) => {
            try {
                const frame = JSON.parse(event.data);
                frameCount++;
                rateWindow.push(Date.now() / 1000);
                frameCountEl.textContent = frameCount + ' frames';

                // Track ID counts
                const id = frame.arbitration_id || 0;
                idCounts[id] = (idCounts[id] || 0) + 1;

                // Console line
                const ts = new Date(frame.timestamp * 1000).toLocaleTimeString();
                appendConsole('[' + ts + '] ' + frame.interface + ' 0x' + id.toString(16).toUpperCase() + ' [' + frame.dlc + '] ' + frame.data);

                // Process decoded signals from SSE
                if (frame.decoded && Array.isArray(frame.decoded)) {
                    for (const sig of frame.decoded) {
                        decodedValues[sig.key] = sig;
                    }
                    // Batch gauge renders to avoid excessive DOM updates
                    if (!gaugeRenderQueued) {
                        gaugeRenderQueued = true;
                        requestAnimationFrame(() => {
                            gaugeRenderQueued = false;
                            if (document.getElementById('gauge-rpm')) {
                                for (const [key, sig] of Object.entries(decodedValues)) {
                                    updateSingleGauge(key, sig);
                                }
                            } else {
                                renderGauges();
                            }
                        });
                    }
                }
            } catch (e) { /* skip bad frames */ }
        };

        eventSource.onerror = () => { /* Auto-reconnect is built into EventSource */ };
    }

    function stopStream() {
        if (stream) {
            stream.close();
            stream = null;
        }
    }

    // ── PAMAS Controls (auto-detect + manual override) ────────────
    if (pamasStart) {
        const pamasPort = document.getElementById('pamas-port');
        const pamasRefresh = document.getElementById('pamas-refresh-ports');
        const pamasModeBadge = document.getElementById('pamas-mode-badge');

        // Fetch available serial ports into dropdown
        async function loadPamasPorts() {
            try {
                const resp = await fetch('/pamas/ports');
                if (!resp.ok) return;
                const ports = await resp.json();
                const current = pamasPort ? pamasPort.value : 'auto';
                if (pamasPort) {
                    pamasPort.innerHTML = '<option value="auto">Auto-detect</option><option value="">Simulation (no device)</option>';
                    for (const p of ports) {
                        const opt = document.createElement('option');
                        opt.value = p.path;
                        opt.textContent = p.path + (p.real_path !== p.path ? ' \u2192 ' + p.real_path : '') + (p.usb ? ' (USB)' : ' (built-in UART)');
                        pamasPort.appendChild(opt);
                    }
                    pamasPort.value = current;
                }
            } catch (e) { /* silent */ }
        }
        loadPamasPorts();
        if (pamasRefresh) pamasRefresh.addEventListener('click', loadPamasPorts);

        // Poll PAMAS status + telemetry every 2s (auto-mode runs from boot)
        async function refreshPamasStatus() {
            try {
                const resp = await fetch('/pamas/status');
                if (!resp.ok) return;
                const s = await resp.json();

                // Update mode badge
                if (pamasModeBadge) {
                    if (s.auto_mode) {
                        if (!s.running) {
                            pamasModeBadge.textContent = 'auto \u2022 no device';
                            pamasModeBadge.className = 'badge bg-secondary ms-2';
                        } else {
                            pamasModeBadge.textContent = 'auto \u2022 ' + s.active_ports.join(', ');
                            pamasModeBadge.className = 'badge bg-info ms-2';
                        }
                    } else {
                        pamasModeBadge.textContent = 'manual \u2022 ' + s.mode;
                        pamasModeBadge.className = 'badge bg-warning text-dark ms-2';
                    }
                    pamasModeBadge.style.fontSize = '0.7rem';
                }

                // Show telemetry if running
                if (s.running) {
                    if (pamasIdle) pamasIdle.style.display = 'none';
                    await refreshPamasTelemetry();
                } else {
                    if (pamasIdle) pamasIdle.style.display = '';
                    if (pamasData) pamasData.innerHTML = '';
                }
            } catch (e) { /* silent */ }
        }

        async function refreshPamasTelemetry() {
            try {
                const resp = await fetch('/pamas/telemetry');
                const devices = await resp.json();
                if (!pamasData || devices.length === 0) return;
                pamasData.innerHTML = devices.map(d => {
                    const counts = d.channel_counts || {};
                    const channelCells = Object.keys(counts).map(ch => `
                        <div class="col-3">${ch.replace('um', '\u00b5m')}: <strong>${counts[ch]}</strong></div>
                    `).join('');
                    const linkInfo = d.link_state
                        ? `<span class="badge bg-secondary ms-1" style="font-size:0.65rem;">${d.link_state} @ ${d.baud}</span>`
                        : '';
                    const proto = d.protocol ? `<small class="text-muted">(${d.protocol})</small>` : '';
                    return `
                    <div class="col-md-6 mb-2">
                        <div class="card">
                            <div class="card-body p-2">
                                <h6 class="mb-1">Device #${d.device_id} <small class="text-muted">${d.port}</small>
                                    <span class="status-dot ${d.connected ? 'active' : 'inactive'} ms-1"></span>
                                    ${linkInfo}
                                </h6>
                                <div class="row small">
                                    <div class="col-6">ISO 4406: <strong>${d.iso_class || '-'}</strong> ${proto}</div>
                                    <div class="col-6">Flow: <strong>${d.flow_rate_ml_min != null ? d.flow_rate_ml_min + ' ml/min' : '-'}</strong></div>
                                    ${channelCells ? `<div class="col-12 mt-1 text-muted">Counts per 100 ml${d.counts_unverified ? ' (unverified mapping)' : ''}:</div>${channelCells}` : ''}
                                    ${d.status ? `<div class="col-12 mt-1">Status: <strong>${d.status}</strong></div>` : ''}
                                    ${d.raw_line ? `<div class="col-12 text-muted text-truncate" title="${d.raw_line}">Last line: <code>${d.raw_line}</code></div>` : ''}
                                    ${!d.iso_class && !channelCells && d.link_state ? `<div class="col-12 text-muted">Listening for data\u2026 check <code>/pamas/raw</code> to inspect the wire format.</div>` : ''}
                                </div>
                            </div>
                        </div>
                    </div>`;
                }).join('');
            } catch (e) { /* silent */ }
        }

        // Start auto-polling immediately (PAMAS watcher runs from boot)
        setInterval(refreshPamasStatus, 2000);
        refreshPamasStatus();

        // Manual Start = override auto-detect with selected port
        pamasStart.addEventListener('click', async () => {
            try {
                const selectedPort = pamasPort ? pamasPort.value : 'auto';
                if (selectedPort === 'auto') {
                    alert('Auto-detect is already running. Select a specific port or Simulation to override.');
                    return;
                }
                const body = selectedPort ? { ports: [selectedPort] } : {};
                const resp = await fetch('/pamas/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                if (resp.ok) {
                    refreshPamasStatus();
                }
            } catch (e) { alert('PAMAS start error: ' + e.message); }
        });

        // Stop = return to auto-detect
        pamasStop.addEventListener('click', async () => {
            try {
                await fetch('/pamas/stop', { method: 'POST' });
                if (pamasPort) pamasPort.value = 'auto';
                refreshPamasStatus();
            } catch (e) { /* ignore */ }
        });
    }
});