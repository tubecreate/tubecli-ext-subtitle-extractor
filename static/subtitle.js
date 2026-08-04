/**
 * Subtitle Extractor — Frontend Logic
 * Handles: Engine selection, file upload, extraction, editing, export, burn, i18n.
 */
(function () {
    'use strict';

    const API_BASE = '/api/v1/subtitle';
    let currentLang = 'en';
    let i18nData = {};
    let subtitles = [];
    let currentFile = null;
    let videoObjectUrl = null;
    let pollTimer = null;

    // ═══ DOM References ═══
    const $ = (s) => document.querySelector(s);
    const $$ = (s) => document.querySelectorAll(s);

    const els = {
        fileInput: $('#file-input'),
        uploadZone: $('#upload-zone'),
        fileInfo: $('#file-info'),
        ytInput: $('#youtube-input'),
        ytUrl: $('#youtube-url'),
        btnExtract: $('#btn-extract'),
        btnAdd: $('#btn-add'),
        btnExport: $('#btn-export'),
        exportMenu: $('#export-menu'),
        btnCopy: $('#btn-copy'),
        btnBurn: $('#btn-burn'),
        progressCard: $('#progress-card'),
        progressBar: $('#progress-bar'),
        progressText: $('#progress-text'),
        videoPlayer: $('#video-player'),
        subtitleOverlay: $('#subtitle-overlay'),
        previewPlaceholder: $('#preview-placeholder'),
        subtitleBody: $('#subtitle-body'),
        editorEmpty: $('#editor-empty'),
        statusText: $('#status-text'),
        statusCount: $('#status-count'),
        videoContainer: $('#video-container'),
        // Loading overlay
        overlay: $('#loading-overlay'),
        overlayTitle: $('#loading-title'),
        overlayBar: $('#loading-progress-bar'),
        overlayPct: $('#loading-pct'),
        overlayDetail: $('#loading-detail'),
    };

    // ═══ Loading Overlay Helpers ═══
    function showOverlay(title, detail) {
        els.overlayTitle.textContent = title || 'Đang xử lý...';
        els.overlayDetail.textContent = detail || '';
        els.overlayBar.style.width = '0%';
        els.overlayPct.textContent = '0%';
        els.overlay.style.display = 'flex';
    }
    function hideOverlay() {
        els.overlay.style.display = 'none';
    }
    function updateOverlay(pct, detail) {
        const p = Math.min(100, Math.max(0, Math.round(pct)));
        els.overlayBar.style.width = `${p}%`;
        els.overlayPct.textContent = `${p}%`;
        if (detail) els.overlayDetail.textContent = detail;
    }

    // ═══ i18n ═══
    async function loadI18n(lang) {
        try {
            const resp = await fetch(`/subtitle-extractor-static/i18n/${lang}.json`);
            if (resp.ok) {
                i18nData = await resp.json();
                currentLang = lang;
                applyI18n();
            }
        } catch (e) {
            console.warn('i18n load failed:', e);
        }
    }

    function t(key) {
        return i18nData[key] || key;
    }

    function applyI18n() {
        $$('[data-i18n]').forEach(el => {
            const key = el.getAttribute('data-i18n');
            if (i18nData[key]) el.textContent = i18nData[key];
        });
        $$('[data-i18n-placeholder]').forEach(el => {
            const key = el.getAttribute('data-i18n-placeholder');
            if (i18nData[key]) el.placeholder = i18nData[key];
        });
        // Update lang buttons
        $$('.lang-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.lang === currentLang);
        });
    }

    // ═══ Engine Selection ═══
    function getEngine() {
        const checked = document.querySelector('input[name="engine"]:checked');
        return checked ? checked.value : 'whisper';
    }

    $$('.engine-card').forEach(card => {
        card.addEventListener('click', () => {
            $$('.engine-card').forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            card.querySelector('input').checked = true;

            const engine = getEngine();
            els.ytInput.style.display = engine === 'youtube' ? 'block' : 'none';
            els.uploadZone.style.display = engine === 'youtube' ? 'none' : 'flex';
            updateExtractButton();
        });
    });

    // ═══ File Upload ═══
    els.uploadZone.addEventListener('click', () => {
        console.log('Upload zone clicked, triggering file input click');
        els.fileInput.click();
    });

    els.uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        els.uploadZone.classList.add('dragover');
    });
    els.uploadZone.addEventListener('dragleave', () => {
        els.uploadZone.classList.remove('dragover');
    });
    els.uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        els.uploadZone.classList.remove('dragover');
        console.log('File dropped:', e.dataTransfer.files[0]);
        if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
    });

    els.fileInput.addEventListener('change', (e) => {
        console.log('File input changed:', e.target.files[0]);
        if (e.target.files[0]) handleFile(e.target.files[0]);
    });

    function handleFile(file) {
        currentFile = file;
        const sizeMB = (file.size / 1024 / 1024).toFixed(1);
        $('#file-info-text').textContent = `📁 ${file.name} (${sizeMB}MB)`;
        els.fileInfo.style.display = 'flex';

        // Show video preview
        if (file.type.startsWith('video') || file.type.startsWith('audio') || file.name.endsWith('.mp4') || file.name.endsWith('.mp3')) {
            if (videoObjectUrl) URL.revokeObjectURL(videoObjectUrl);
            videoObjectUrl = URL.createObjectURL(file);
            els.videoPlayer.src = videoObjectUrl;
            els.videoPlayer.style.display = 'block';
            els.previewPlaceholder.style.display = 'none';
        }

        updateExtractButton();
    }

    function clearFile() {
        currentFile = null;
        els.fileInput.value = '';
        els.fileInfo.style.display = 'none';
        $('#file-info-text').textContent = '';
        if (videoObjectUrl) {
            URL.revokeObjectURL(videoObjectUrl);
            videoObjectUrl = null;
        }
        els.videoPlayer.src = '';
        els.videoPlayer.style.display = '';
        els.previewPlaceholder.style.display = 'flex';
        updateExtractButton();
    }

    $('#btn-remove-file').addEventListener('click', (e) => {
        e.stopPropagation();
        clearFile();
    });

    function updateExtractButton() {
        const engine = getEngine();
        if (engine === 'youtube') {
            els.btnExtract.disabled = false;
        } else {
            els.btnExtract.disabled = !currentFile;
        }
    }

    // ═══ Translate Toggle ═══
    $('#chk-translate').addEventListener('change', (e) => {
        $('#select-translate-to').style.display = e.target.checked ? 'block' : 'none';
    });

    // ═══ Extraction ═══
    els.btnExtract.addEventListener('click', handleExtract);

    async function handleExtract() {
        const engine = getEngine();
        els.btnExtract.disabled = true;
        els.progressCard.style.display = 'block';
        els.progressBar.style.width = '0%';

        const engineLabel = engine === 'whisper' ? 'Whisper (Local AI)' : engine === 'gemini' ? 'Gemini (Cloud)' : 'YouTube CC';
        showOverlay(`🚀 Đang tách phụ đề — ${engineLabel}`, 'Khởi tạo...');

        try {
            if (engine === 'youtube') {
                const url = els.ytUrl.value.trim();
                if (!url) { alert(t('error.no_file')); hideOverlay(); els.btnExtract.disabled = false; return; }

                updateOverlay(20, 'Đang tải phụ đề từ YouTube...');
                const resp = await fetch(`${API_BASE}/extract/youtube`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, languages: getSelectedLanguages() }),
                });
                const data = await resp.json();
                if (data.success) {
                    subtitles = data.subtitles || [];
                    renderSubtitles();
                    completeExtraction(subtitles.length);
                    // Re-enable on the SUCCESS path too. Only the failure path
                    // did it, so a YouTube CC run that worked left the Extract
                    // button greyed out until the page was reloaded.
                    els.btnExtract.disabled = false;
                } else {
                    hideOverlay();
                    setStatus('error', data.message || 'Error');
                    els.btnExtract.disabled = false;
                }
            } else {
                updateOverlay(10, `Đang tải file lên server (${(currentFile.size / 1024 / 1024).toFixed(1)} MB)...`);
                const filePath = await uploadFile(currentFile);
                if (!filePath) { hideOverlay(); els.btnExtract.disabled = false; return; }

                updateOverlay(20, `Đang gửi yêu cầu tách phụ đề...`);

                const language = $('#select-lang').value || null;
                const translateTo = $('#chk-translate').checked ? $('#select-translate-to').value : null;

                const resp = await fetch(`${API_BASE}/extract`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        file_path: filePath,
                        engine,
                        language,
                        translate_to: translateTo,
                    }),
                });
                const data = await resp.json();
                if (data.task_id) {
                    updateOverlay(25, `Đang xử lý... (Task: ${data.task_id})`);
                    pollTaskStatus(data.task_id);
                } else {
                    hideOverlay();
                    setStatus('error', 'Không nhận được task_id');
                    els.btnExtract.disabled = false;
                }
            }
        } catch (e) {
            hideOverlay();
            setStatus('error', `Error: ${e.message}`);
            els.btnExtract.disabled = false;
        }
    }

    async function uploadFile(file) {
        // Save file to server data dir
        const formData = new FormData();
        formData.append('file', file);
        try {
            const resp = await fetch(`${API_BASE}/upload`, { method: 'POST', body: formData });
            const data = await resp.json();
            return data.path || data.file_path || null;
        } catch (e) {
            // Fallback: use local path if file came from server
            console.warn('Upload failed, trying local path');
            return null;
        }
    }

    let _pollCount = 0;
    let _lastPhase = 'extract'; // 'extract' or 'translate'
    function pollTaskStatus(taskId) {
        if (pollTimer) clearInterval(pollTimer);
        _pollCount = 0;
        _lastPhase = 'extract';
        pollTimer = setInterval(async () => {
            try {
                _pollCount++;
                const resp = await fetch(`${API_BASE}/status/${taskId}`);
                // Stop on an HTTP error. The server answers 404 "Task not
                // found" with a body of {"detail": ...} and no "status" key,
                // so neither the success nor the error branch below could ever
                // fire — the interval ran forever and left the user stuck
                // behind a full-screen overlay that has no close button.
                if (!resp.ok) {
                    clearInterval(pollTimer);
                    hideOverlay();
                    let detail = '';
                    try { detail = (await resp.json()).detail || ''; } catch (e) { /* not JSON */ }
                    setStatus('error', detail || `Máy chủ trả lỗi ${resp.status} khi hỏi tiến độ.`);
                    els.btnExtract.disabled = false;
                    return;
                }
                const data = await resp.json();

                if (data.total > 0 && data.total > 100) {
                    // Likely translation phase (total = number of subtitle lines)
                    _lastPhase = 'translate';
                    const pct = 60 + Math.round((data.progress / data.total) * 35);
                    els.progressBar.style.width = `${pct}%`;
                    els.progressText.textContent = `${data.progress} / ${data.total}`;
                    updateOverlay(pct, `🌐 Đang dịch thuật: ${data.progress}/${data.total} dòng...`);
                } else if (data.total > 0) {
                    const pct = 25 + Math.round((data.progress / data.total) * 35);
                    els.progressBar.style.width = `${pct}%`;
                    els.progressText.textContent = `${data.progress} / ${data.total}`;
                    updateOverlay(pct, `Đang xử lý: ${data.progress}/${data.total} đoạn...`);
                } else {
                    // Indeterminate — slow ramp
                    const fakePct = Math.min(55, 25 + _pollCount * 2);
                    updateOverlay(fakePct, `Đang phân tích âm thanh... (${_pollCount * 2}s)`);
                }

                if (data.status === 'success' && data.result) {
                    clearInterval(pollTimer);
                    updateOverlay(95, 'Hoàn tất! Đang render kết quả...');
                    subtitles = data.result.subtitles || [];
                    renderSubtitles();
                    completeExtraction(subtitles.length);
                    els.btnExtract.disabled = false;
                } else if (data.status === 'error') {
                    clearInterval(pollTimer);
                    hideOverlay();
                    setStatus('error', data.result?.message || 'Error');
                    els.btnExtract.disabled = false;
                }
            } catch (e) {
                console.error('Poll error:', e);
            }
        }, 2000);
    }

    function completeExtraction(count) {
        updateOverlay(100, `✅ Hoàn tất — ${count} dòng phụ đề`);
        els.progressBar.style.width = '100%';
        setStatus('complete', t('status.complete').replace('{count}', count));
        els.statusCount.textContent = `${count} subtitles`;
        // Auto-hide overlay after brief celebration
        setTimeout(() => hideOverlay(), 1500);
    }

    function getSelectedLanguages() {
        const lang = $('#select-lang').value;
        return lang ? [lang] : null;
    }

    // ═══ Subtitle Rendering ═══
    function renderSubtitles() {
        const body = els.subtitleBody;
        body.innerHTML = '';
        els.editorEmpty.style.display = subtitles.length ? 'none' : 'flex';

        subtitles.forEach((sub, i) => {
            const tr = document.createElement('tr');
            tr.id = `sub-${i}`;
            tr.innerHTML = `
                <td class="col-index">${i + 1}</td>
                <td class="col-time">
                    <input class="time-input" type="text" value="${formatTime(sub.start)}" data-idx="${i}" data-field="start">
                </td>
                <td class="col-time">
                    <input class="time-input" type="text" value="${formatTime(sub.end)}" data-idx="${i}" data-field="end">
                </td>
                <td>
                    <textarea class="text-input" rows="1" data-idx="${i}">${escapeHtml(sub.text)}</textarea>
                </td>
                <td class="col-actions">
                    <div class="row-actions">
                        <button class="row-btn" title="${t('action.split')}" data-action="split" data-idx="${i}">✂️</button>
                        <button class="row-btn" title="${t('action.delete')}" data-action="delete" data-idx="${i}">🗑️</button>
                    </div>
                </td>
            `;

            // Click to seek video
            tr.addEventListener('click', (e) => {
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'BUTTON') return;
                els.videoPlayer.currentTime = sub.start;
            });

            body.appendChild(tr);
        });

        // Bind edit events
        body.querySelectorAll('.time-input').forEach(input => {
            input.addEventListener('change', (e) => {
                const idx = parseInt(e.target.dataset.idx);
                const field = e.target.dataset.field;
                subtitles[idx][field] = parseTime(e.target.value);
            });
        });

        body.querySelectorAll('.text-input').forEach(ta => {
            ta.addEventListener('input', (e) => {
                const idx = parseInt(e.target.dataset.idx);
                subtitles[idx].text = e.target.value;
                // Auto-resize
                e.target.style.height = 'auto';
                e.target.style.height = e.target.scrollHeight + 'px';
            });
        });

        body.querySelectorAll('.row-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx);
                if (btn.dataset.action === 'split') splitSubtitle(idx);
                else if (btn.dataset.action === 'delete') deleteSubtitle(idx);
            });
        });
    }

    // ═══ Editor Actions ═══
    function splitSubtitle(idx) {
        const sub = subtitles[idx];
        const mid = (sub.start + sub.end) / 2;
        const words = sub.text.split(' ');
        const midWord = Math.ceil(words.length / 2);

        subtitles.splice(idx, 1,
            { start: sub.start, end: mid, text: words.slice(0, midWord).join(' ') },
            { start: mid, end: sub.end, text: words.slice(midWord).join(' ') }
        );
        renderSubtitles();
    }

    function deleteSubtitle(idx) {
        subtitles.splice(idx, 1);
        renderSubtitles();
    }

    els.btnAdd.addEventListener('click', () => {
        const time = els.videoPlayer.currentTime || 0;
        subtitles.push({ start: time, end: time + 3, text: 'New subtitle' });
        subtitles.sort((a, b) => a.start - b.start);
        renderSubtitles();
    });

    // ═══ Video Time Sync ═══
    els.videoPlayer.addEventListener('timeupdate', () => {
        const t = els.videoPlayer.currentTime;
        const active = subtitles.find(s => t >= s.start && t < s.end);

        if (active) {
            els.subtitleOverlay.textContent = active.text;
            els.subtitleOverlay.style.display = 'block';
        } else {
            els.subtitleOverlay.style.display = 'none';
        }

        // Highlight active row
        $$('.subtitle-table tr.active').forEach(tr => tr.classList.remove('active'));
        const idx = subtitles.findIndex(s => t >= s.start && t < s.end);
        if (idx >= 0) {
            const row = $(`#sub-${idx}`);
            if (row) {
                row.classList.add('active');
                row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }
    });

    // ═══ Export ═══
    els.btnExport.addEventListener('click', (e) => {
        e.stopPropagation();
        els.exportMenu.classList.toggle('open');
    });
    document.addEventListener('click', () => els.exportMenu.classList.remove('open'));

    els.exportMenu.querySelectorAll('button').forEach(btn => {
        btn.addEventListener('click', async () => {
            const format = btn.dataset.format;
            try {
                const resp = await fetch(`${API_BASE}/export`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ subtitles, format }),
                });
                const data = await resp.json();
                if (data.success) {
                    setStatus('complete', `Exported to ${data.path}`);
                    // Trigger download
                    downloadContent(generateContent(format), `subtitles.${format}`);
                }
            } catch (e) {
                setStatus('error', `Export error: ${e.message}`);
            }
        });
    });

    els.btnCopy.addEventListener('click', () => {
        const srt = generateSRT();
        navigator.clipboard.writeText(srt).then(() => {
            setStatus('complete', 'Copied to clipboard!');
        });
    });

    // ═══ Burn ═══
    els.btnBurn.addEventListener('click', async () => {
        if (!currentFile || !subtitles.length) {
            alert(t('error.no_file'));
            return;
        }

        els.btnBurn.disabled = true;
        showOverlay('🔥 Ghi phụ đề vào video', 'Đang export SRT...');
        updateOverlay(10, 'Đang export SRT...');

        try {
            const resp = await fetch(`${API_BASE}/export`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ subtitles, format: 'srt' }),
            });
            const exportData = await resp.json();
            if (!exportData.success) { hideOverlay(); els.btnBurn.disabled = false; return; }

            updateOverlay(25, 'Đang tải video lên server...');
            const filePath = await uploadFile(currentFile);

            updateOverlay(40, 'Đang ghi phụ đề bằng FFmpeg...');
            const burnResp = await fetch(`${API_BASE}/burn`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    video_path: filePath,
                    srt_path: exportData.path,
                }),
            });
            const burnData = await burnResp.json();
            if (burnData.task_id) {
                pollBurnStatus(burnData.task_id);
            }
        } catch (e) {
            hideOverlay();
            els.btnBurn.disabled = false;
            setStatus('error', t('error.burn_failed'));
        }
    });

    function pollBurnStatus(taskId) {
        let burnPoll = 0;
        const timer = setInterval(async () => {
            burnPoll++;
            const fakePct = Math.min(95, 40 + burnPoll * 5);
            updateOverlay(fakePct, `FFmpeg đang xử lý... (${burnPoll * 3}s)`);

            const resp = await fetch(`${API_BASE}/status/${taskId}`);
            const data = await resp.json();
            if (data.status === 'success') {
                clearInterval(timer);
                updateOverlay(100, '✅ Ghi phụ đề hoàn tất!');
                setStatus('complete', t('status.burn_done'));
                setTimeout(() => { hideOverlay(); els.btnBurn.disabled = false; }, 1500);
            } else if (data.status === 'error') {
                clearInterval(timer);
                hideOverlay();
                els.btnBurn.disabled = false;
                setStatus('error', data.result?.message || t('error.burn_failed'));
            }
        }, 3000);
    }

    // ═══ Helpers ═══
    // Rounded, not truncated, and it must match _format_srt_time on the
    // server byte for byte. Math.floor((seconds % 1) * 1000) turned 1.4 into
    // ,399 and 8.1 into ,099 because a binary float sits just under the value
    // you typed — every cue landed up to a millisecond early.
    function formatTime(seconds) {
        const v = Number(seconds);
        if (!isFinite(v) || v < 0) return '00:00:00,000';
        const totalMs = Math.round(v * 1000);
        const ms = totalMs % 1000;
        const totalS = Math.floor(totalMs / 1000);
        const h = Math.floor(totalS / 3600);
        const m = Math.floor((totalS % 3600) / 60);
        const s = totalS % 60;
        return `${pad(h)}:${pad(m)}:${pad(s)},${pad3(ms)}`;
    }

    function parseTime(str) {
        if (!str) return 0;
        const [hms, ms] = str.split(',');
        const [h, m, s] = (hms || '').split(':');
        return (parseInt(h) || 0) * 3600 + (parseInt(m) || 0) * 60 + (parseInt(s) || 0) + (parseInt(ms) || 0) / 1000;
    }

    function pad(n) { return String(n).padStart(2, '0'); }
    function pad3(n) { return String(n).padStart(3, '0'); }
    function escapeHtml(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

    function generateSRT() {
        return subtitles.map((s, i) => `${i + 1}\n${formatTime(s.start)} --> ${formatTime(s.end)}\n${s.text}`).join('\n\n');
    }

    // h:mm:ss.cc — ASS uses one-digit hours and CENTISECONDS, not the
    // comma-and-milliseconds of SRT. Mirrors _to_ass() on the server so the
    // two exports cannot drift.
    function assTime(sec) {
        const v = Math.max(0, Number(sec) || 0);
        const h = Math.floor(v / 3600);
        const m = Math.floor((v % 3600) / 60);
        const s = Math.floor(v % 60);
        const cs = Math.round((v - Math.floor(v)) * 100);
        return `${h}:${pad(m)}:${pad(s)}.${pad(cs > 99 ? 99 : cs)}`;
    }

    function generateASS() {
        const header = [
            '[Script Info]', 'ScriptType: v4.00+', 'PlayResX: 1920', 'PlayResY: 1080', '',
            '[V4+ Styles]',
            'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, ' +
            'BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, ' +
            'BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
            'Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,' +
            '100,100,0,0,1,2,1,2,10,10,40,1', '',
            '[Events]',
            'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text', '',
        ].join('\n');
        const events = subtitles.map(s =>
            `Dialogue: 0,${assTime(s.start)},${assTime(s.end)},Default,,0,0,0,,` +
            String(s.text || '').replace(/\r?\n/g, '\\N')
        ).join('\n');
        return header + events + '\n';
    }

    function generateContent(format) {
        if (format === 'srt') return generateSRT();
        if (format === 'json') return JSON.stringify(subtitles, null, 2);
        if (format === 'vtt') {
            return 'WEBVTT\n\n' + subtitles.map(s =>
                `${formatTime(s.start).replace(',', '.')} --> ${formatTime(s.end).replace(',', '.')}\n${s.text}`
            ).join('\n\n');
        }
        // 'ass' used to fall through to here and hand back SRT under an .ass
        // filename, which no player would load.
        if (format === 'ass') return generateASS();
        return generateSRT();
    }

    function downloadContent(content, filename) {
        const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
    }

    function setStatus(type, text) {
        els.statusText.textContent = text;
        // 'error' used to fall to '' — no class at all — so a failure rendered
        // in the same muted colour as idle text at the very bottom of the
        // page, which is the least prominent place on screen for the one
        // message the user has to read.
        els.statusText.className =
            type === 'processing' ? 'status-processing'
            : type === 'error' ? 'status-error'
            : type === 'success' ? 'status-success' : '';
    }

    // ═══ Init ═══
    // Language buttons
    $$('.lang-btn').forEach(btn => {
        btn.addEventListener('click', () => loadI18n(btn.dataset.lang));
    });

    // Detect UI language from TubeCLI
    (async function init() {
        try {
            const resp = await fetch('/api/v1/settings/language');
            const data = await resp.json();
            const lang = data?.language || 'en';
            await loadI18n(lang);
            
            // Auto focus the target subtitle language
            const langSelect = $('#select-lang');
            if (langSelect) {
                // If it exists in the options
                if (Array.from(langSelect.options).some(o => o.value === lang)) {
                    langSelect.value = lang;
                }
            }
        } catch {
            await loadI18n('en');
        }
    })();

})();
