// ── Trinitron: tradução de tela via Google Gemini ────────────────────────────
// Captura o frame limpo do <video>, pede OCR + tradução (PT-BR) + posições ao
// Gemini numa chamada só, e mostra o resultado como overlay sobre o canvas ou
// num painel lateral. Disparo manual apenas: botão ou tecla T.

(function () {
	const DEFAULT_MODEL = 'gemini-flash-latest'; // melhor localização (grounding) das caixas
	const MAX_CAPTURE_WIDTH = 1280; // reduz payload/custo sem perder legibilidade

	// Fingerprint de frame (cache de reuso) — mesma heurística de grade da Fase 2.
	const DET_W = 320, DET_H = 180;   // resolução de análise do fingerprint
	const GRID_X = 40, GRID_Y = 22;   // grade fina (pega mudança de texto pequeno)
	const CACHE_MAX = 12;             // entradas guardadas por sessão
	const CELL_DELTA = 8;             // variação de luminância (0–255) p/ célula "mudar"
	const CELL_TOLERANCE = 3;         // nº de células que podem mudar e ainda ser "mesma tela"
	const LS = {
		key: 'trinitron.geminiKey',
		model: 'trinitron.geminiModel',
		enabled: 'trinitron.translateEnabled',
		cache: 'trinitron.cacheEnabled',
		panelMode: 'trinitron.transPanelMode',
		font: 'trinitron.transFont',
		preset: 'trinitron.transPreset',
		fontScale: 'trinitron.transFontScale',
		bgOpacity: 'trinitron.transBgOpacity',
		borderWidth: 'trinitron.transBorderWidth',
		hideAfter: 'trinitron.transHideAfter',
		usageDay: 'trinitron.usageDay',
		limits: 'trinitron.usageLimits'
	};

	// Combinações de cores prontas: cor da fonte, fundo e borda.
	const PRESETS = [
		{ id: 'classico', name: 'Clássico (branco/preto)', fg: '#ffffff', bg: '#000000', border: '#000000' },
		{ id: 'amarelo', name: 'Amarelo retrô', fg: '#ffe14d', bg: '#0d0d0d', border: '#000000' },
		{ id: 'verde', name: 'Verde fósforo', fg: '#6dff6d', bg: '#001400', border: '#001a00' },
		{ id: 'ciano', name: 'Ciano CRT', fg: '#5ff0ff', bg: '#001426', border: '#00060f' },
		{ id: 'ambar', name: 'Âmbar', fg: '#ffb000', bg: '#160c00', border: '#000000' },
		{ id: 'rosa', name: 'Rosa neon', fg: '#ff6ec7', bg: '#1a0020', border: '#33002a' },
		{ id: 'branco', name: 'Preto no branco', fg: '#141414', bg: '#ffffff', border: '#ffffff' },
		{ id: 'vermelho', name: 'Vermelho alerta', fg: '#ff5a5a', bg: '#160000', border: '#000000' }
	];

	const video = document.getElementById('video');
	const overlay = document.getElementById('translationOverlay');
	const panel = document.getElementById('translationPanel');
	const keyInput = document.getElementById('geminiKey');
	const modelInput = document.getElementById('geminiModel');
	const enabledInput = document.getElementById('translateEnabled');
	const cacheEnabledInput = document.getElementById('cacheEnabled');
	const translateBtn = document.getElementById('translateBtn');
	const peekBtn = document.getElementById('translatePeekBtn');
	const clearBtn = document.getElementById('translateClearBtn');
	const panelModeInput = document.getElementById('transPanelMode');
	const fontInput = document.getElementById('transFont');
	const presetInput = document.getElementById('transPreset');
	const fontScaleInput = document.getElementById('transFontScale');
	const fontScaleValEl = document.getElementById('transFontScaleVal');
	const bgOpacityInput = document.getElementById('transBgOpacity');
	const borderWidthInput = document.getElementById('transBorderWidth');
	const borderValEl = document.getElementById('transBorderVal');
	const hideAfterInput = document.getElementById('transHideAfter');
	const hideValEl = document.getElementById('transHideVal');
	const statusEl = document.getElementById('translateStatus');
	const indicator = document.getElementById('translateIndicator');
	const sidebar = document.getElementById('sidebar');
	const usoPanel = document.querySelector('.subtab-panel[data-subpanel="uso"]');
	const uReqDay = document.getElementById('uReqDay');
	const uRpd = document.getElementById('uRpd');
	const uBarRpd = document.getElementById('uBarRpd');
	const uRpm = document.getElementById('uRpm');
	const uRpmLim = document.getElementById('uRpmLim');
	const uBarRpm = document.getElementById('uBarRpm');
	const uTpm = document.getElementById('uTpm');
	const uTpmLim = document.getElementById('uTpmLim');
	const uBarTpm = document.getElementById('uBarTpm');
	const uReal = document.getElementById('uReal');
	const uHit = document.getElementById('uHit');
	const limRpd = document.getElementById('limRpd');
	const limRpm = document.getElementById('limRpm');
	const limTpm = document.getElementById('limTpm');
	const usageResetBtn = document.getElementById('usageResetBtn');

	let busy = false;
	let lastItems = [];
	let hideTimer = null;
	let visible = false;   // tradução atualmente na tela?
	let peeking = false;   // botão "segurar para ver" pressionado?
	let wasVisible = false;
	let detCanvas = null, detCtx = null; // canvas minúsculo p/ fingerprint
	const frameCache = [];               // LRU: { grid, items }
	const callLog = [];                  // {t, tokens} das chamadas reais (janela 60s)
	let usageDay = { day: '', reqDay: 0, hitDay: 0 };
	let limits = { rpd: 1000, rpm: 15, tpm: 250000 };

	PRESETS.forEach(p => {
		const o = document.createElement('option');
		o.value = p.id;
		o.textContent = p.name;
		presetInput.appendChild(o);
	});

	function theme() {
		return PRESETS.find(p => p.id === presetInput.value) || PRESETS[0];
	}

	// ── Abas do menu ──────────────────────────────────────────────────────────
	document.querySelectorAll('.tab-btn').forEach(btn => {
		btn.addEventListener('click', () => {
			const tab = btn.dataset.tab;
			document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
			document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.dataset.panel === tab));
		});
	});

	// Sub-abas dentro da aba Tradução.
	document.querySelectorAll('.subtab-btn').forEach(btn => {
		btn.addEventListener('click', () => {
			const sub = btn.dataset.subtab;
			document.querySelectorAll('.subtab-btn').forEach(b => b.classList.toggle('active', b === btn));
			document.querySelectorAll('.subtab-panel').forEach(p => p.classList.toggle('active', p.dataset.subpanel === sub));
		});
	});

	// ── Persistência simples no navegador ────────────────────────────────────
	function loadPrefs() {
		keyInput.value = localStorage.getItem(LS.key) || '';
		modelInput.value = localStorage.getItem(LS.model) || DEFAULT_MODEL;
		enabledInput.checked = localStorage.getItem(LS.enabled) === 'true';
		cacheEnabledInput.checked = localStorage.getItem(LS.cache) !== 'false'; // ligado por padrão
		panelModeInput.checked = localStorage.getItem(LS.panelMode) === 'true';
		fontInput.value = localStorage.getItem(LS.font) || 'monospace';
		if (!fontInput.value) fontInput.value = 'monospace'; // valor salvo antigo já removido → 1ª opção
		presetInput.value = localStorage.getItem(LS.preset) || 'classico';
		fontScaleInput.value = localStorage.getItem(LS.fontScale) || '0.8';
		fontScaleValEl.textContent = fontScaleInput.value;
		bgOpacityInput.value = localStorage.getItem(LS.bgOpacity) || '0.75';
		borderWidthInput.value = localStorage.getItem(LS.borderWidth) || '0';
		borderValEl.textContent = borderWidthInput.value;
		hideAfterInput.value = localStorage.getItem(LS.hideAfter) || '0';
		hideValEl.textContent = hideAfterInput.value;
		try { usageDay = JSON.parse(localStorage.getItem(LS.usageDay)) || usageDay; } catch (e) { /* ignore */ }
		try { limits = Object.assign(limits, JSON.parse(localStorage.getItem(LS.limits)) || {}); } catch (e) { /* ignore */ }
		rollDay();
		limRpd.value = limits.rpd;
		limRpm.value = limits.rpm;
		limTpm.value = limits.tpm;
		refreshUsage();
	}

	function save(key, val) { localStorage.setItem(key, val); }
	function rerender() { if (visible) render(lastItems); }

	keyInput.addEventListener('change', () => save(LS.key, keyInput.value.trim()));
	modelInput.addEventListener('change', () => save(LS.model, modelInput.value.trim()));
	enabledInput.addEventListener('change', () => save(LS.enabled, enabledInput.checked));
	cacheEnabledInput.addEventListener('change', () => save(LS.cache, cacheEnabledInput.checked));
	panelModeInput.addEventListener('change', () => { save(LS.panelMode, panelModeInput.checked); rerender(); });
	fontInput.addEventListener('change', () => { save(LS.font, fontInput.value); rerender(); });
	presetInput.addEventListener('change', () => { save(LS.preset, presetInput.value); rerender(); });
	fontScaleInput.addEventListener('input', () => { save(LS.fontScale, fontScaleInput.value); fontScaleValEl.textContent = fontScaleInput.value; rerender(); });
	bgOpacityInput.addEventListener('input', () => { save(LS.bgOpacity, bgOpacityInput.value); rerender(); });
	borderWidthInput.addEventListener('input', () => { save(LS.borderWidth, borderWidthInput.value); borderValEl.textContent = borderWidthInput.value; rerender(); });
	hideAfterInput.addEventListener('input', () => { save(LS.hideAfter, hideAfterInput.value); hideValEl.textContent = hideAfterInput.value; });
	limRpd.addEventListener('input', () => { limits.rpd = parseInt(limRpd.value, 10) || 1; saveLimits(); refreshUsage(); });
	limRpm.addEventListener('input', () => { limits.rpm = parseInt(limRpm.value, 10) || 1; saveLimits(); refreshUsage(); });
	limTpm.addEventListener('input', () => { limits.tpm = parseInt(limTpm.value, 10) || 1; saveLimits(); refreshUsage(); });
	usageResetBtn.addEventListener('click', () => {
		usageDay = { day: pacificDay(), reqDay: 0, hitDay: 0 };
		callLog.length = 0;
		saveUsage();
		refreshUsage();
	});

	function setStatus(msg, isError) {
		statusEl.textContent = msg || '';
		statusEl.classList.toggle('error', !!isError);
	}

	// ── Captura o frame limpo do vídeo (sem filtro CRT → melhor OCR) ──────────
	function captureFrame() {
		const vw = video.videoWidth, vh = video.videoHeight;
		if (!vw || !vh) return null;
		const scale = Math.min(1, MAX_CAPTURE_WIDTH / vw);
		const w = Math.round(vw * scale), h = Math.round(vh * scale);
		const c = document.createElement('canvas');
		c.width = w; c.height = h;
		c.getContext('2d').drawImage(video, 0, 0, w, h);
		return c.toDataURL('image/jpeg', 0.85);
	}

	// ── Cache de frames: reusa tradução quando a tela repete (sem chamar a API) ──
	// Impressão digital = grade de luminância média do frame (do vídeo limpo).
	function frameFingerprint() {
		const vw = video.videoWidth, vh = video.videoHeight;
		if (!vw || !vh) return null;
		if (!detCanvas) {
			detCanvas = document.createElement('canvas');
			detCanvas.width = DET_W; detCanvas.height = DET_H;
			detCtx = detCanvas.getContext('2d', { willReadFrequently: true });
		}
		detCtx.drawImage(video, 0, 0, DET_W, DET_H);
		const data = detCtx.getImageData(0, 0, DET_W, DET_H).data;
		const grid = new Float32Array(GRID_X * GRID_Y);
		const counts = new Int32Array(GRID_X * GRID_Y);
		for (let y = 0; y < DET_H; y++) {
			const gy = (y * GRID_Y / DET_H) | 0;
			for (let x = 0; x < DET_W; x++) {
				const gx = (x * GRID_X / DET_W) | 0;
				const lum = data[(y * DET_W + x) * 4] * 0.299 + data[(y * DET_W + x) * 4 + 1] * 0.587 + data[(y * DET_W + x) * 4 + 2] * 0.114;
				const c = gy * GRID_X + gx;
				grid[c] += lum;
				counts[c]++;
			}
		}
		for (let c = 0; c < grid.length; c++) grid[c] /= counts[c] || 1;
		return grid;
	}

	// "Mesma tela" = quase nenhuma célula mudou além do limiar. Usar contagem de
	// células (não média) evita que uma mudança pequena de texto seja diluída.
	function sameScreen(a, b) {
		if (!a || !b || a.length !== b.length) return false;
		let changed = 0;
		for (let i = 0; i < a.length; i++) {
			if (Math.abs(a[i] - b[i]) > CELL_DELTA && ++changed > CELL_TOLERANCE) return false;
		}
		return true;
	}

	// Procura no cache uma tela idêntica; devolve a entrada (e a promove no LRU).
	function cacheLookup(grid) {
		if (!grid) return null;
		for (let i = 0; i < frameCache.length; i++) {
			if (sameScreen(grid, frameCache[i].grid)) {
				const [entry] = frameCache.splice(i, 1);
				frameCache.unshift(entry);
				return entry;
			}
		}
		return null;
	}

	function cacheStore(grid, items) {
		if (!grid) return;
		frameCache.unshift({ grid, items });
		if (frameCache.length > CACHE_MAX) frameCache.length = CACHE_MAX;
	}

	// ── Contadores de uso da API (local) ─────────────────────────────────────
	function pacificDay() {
		return new Date().toLocaleDateString('en-CA', { timeZone: 'America/Los_Angeles' });
	}

	function saveUsage() { localStorage.setItem(LS.usageDay, JSON.stringify(usageDay)); }
	function saveLimits() { localStorage.setItem(LS.limits, JSON.stringify(limits)); }

	function rollDay() {
		const d = pacificDay();
		if (usageDay.day !== d) {
			usageDay = { day: d, reqDay: 0, hitDay: 0 };
			saveUsage();
		}
	}

	function recordCall(usage) {
		rollDay();
		callLog.push({ t: Date.now(), tokens: (usage && usage.totalTokenCount) || 0 });
		usageDay.reqDay++;
		saveUsage();
		refreshUsage();
	}

	function recordHit() {
		rollDay();
		usageDay.hitDay++;
		saveUsage();
		refreshUsage();
	}

	function fmt(n) {
		return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
	}

	function setBar(bar, valEl, val, lim, useFmt) {
		valEl.textContent = useFmt ? fmt(val) : val;
		const pct = lim > 0 ? Math.min(100, val / lim * 100) : 0;
		bar.style.width = pct + '%';
		bar.classList.toggle('warn', pct >= 90);
	}

	function refreshUsage() {
		rollDay();
		const now = Date.now();
		while (callLog.length && now - callLog[0].t > 60000) callLog.shift();
		const rpm = callLog.length;
		const tpm = callLog.reduce((s, e) => s + e.tokens, 0);
		setBar(uBarRpd, uReqDay, usageDay.reqDay, limits.rpd);
		setBar(uBarRpm, uRpm, rpm, limits.rpm);
		setBar(uBarTpm, uTpm, tpm, limits.tpm, true);
		uRpd.textContent = limits.rpd;
		uRpmLim.textContent = limits.rpm;
		uTpmLim.textContent = fmt(limits.tpm);
		uReal.textContent = usageDay.reqDay;
		uHit.textContent = usageDay.hitDay;
	}

	// ── Chamada ao Gemini com saída estruturada (JSON) ───────────────────────
	const PROMPT =
		'Você é um tradutor de tela de videogame em tempo real. Analise a imagem e ' +
		'detecte TODOS os blocos de texto legível (japonês ou inglês). Para cada bloco forneça: ' +
		'"original" (texto detectado), "pt" (tradução natural para português do Brasil no contexto de um jogo), ' +
		'e a caixa delimitadora como frações da imagem entre 0 e 1: "x","y" (canto superior esquerdo), ' +
		'"w","h" (largura e altura). Ignore texto decorativo ilegível, logos e marcas d\'água. ' +
		'Ignore também blocos que sejam apenas números, placares, cronômetros ou fórmulas ' +
		'(ex.: "355", "x5", "10%", "1/2") — só traduza texto que tenha palavras. ' +
		'Se não houver texto, retorne uma lista vazia.';

	const RESPONSE_SCHEMA = {
		type: 'ARRAY',
		items: {
			type: 'OBJECT',
			properties: {
				original: { type: 'STRING' },
				pt: { type: 'STRING' },
				x: { type: 'NUMBER' },
				y: { type: 'NUMBER' },
				w: { type: 'NUMBER' },
				h: { type: 'NUMBER' }
			},
			required: ['pt', 'x', 'y', 'w', 'h']
		}
	};

	async function callGemini(dataUrl, apiKey) {
		const base64 = dataUrl.split(',')[1];
		const model = modelInput.value.trim() || DEFAULT_MODEL;
		const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${encodeURIComponent(apiKey)}`;
		const body = {
			contents: [{
				parts: [
					{ text: PROMPT },
					{ inline_data: { mime_type: 'image/jpeg', data: base64 } }
				]
			}],
			generationConfig: {
				temperature: 0,
				maxOutputTokens: 2048,
				thinkingConfig: { thinkingBudget: 0 },
				responseMimeType: 'application/json',
				responseSchema: RESPONSE_SCHEMA
			}
		};
		const res = await fetch(url, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body)
		});
		if (!res.ok) {
			const detail = await res.text().catch(() => '');
			throw new Error(`HTTP ${res.status} — ${detail.slice(0, 200)}`);
		}
		const json = await res.json();
		const text = json.candidates?.[0]?.content?.parts?.[0]?.text;
		if (!text) throw new Error('Resposta vazia da API');
		return { items: JSON.parse(text), usage: json.usageMetadata || null };
	}

	// True se o texto tem palavras reais — descarta blocos só de número/fórmula.
	const FORMULA_RE = /^[\s\d.,:;xX×*+\-/=%()\[\]#°]+$/;
	function isMeaningful(text) {
		const t = (text || '').trim();
		if (!t || FORMULA_RE.test(t)) return false;
		return /\p{L}/u.test(t);
	}

	// Alguns modelos devolvem coordenadas em escala 0–1000. Normaliza p/ 0–1.
	function norm(v) {
		if (typeof v !== 'number' || isNaN(v)) return 0;
		if (v > 1.5) v = v / 1000;
		return Math.min(1, Math.max(0, v));
	}

	function hexToRgba(hex, alpha) {
		const n = parseInt(hex.slice(1), 16);
		const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
		return `rgba(${r}, ${g}, ${b}, ${alpha})`;
	}

	// Aplica cor, fonte e borda (contorno) configuráveis a um elemento de texto.
	function styleText(el) {
		const t = theme();
		el.style.color = t.fg;
		el.style.fontFamily = fontInput.value;
		const bw = parseFloat(borderWidthInput.value) || 0;
		if (bw > 0) {
			el.style.webkitTextStroke = bw + 'px ' + t.border;
			el.style.textShadow = 'none';
		} else {
			el.style.webkitTextStroke = '0';
			el.style.textShadow = '0 1px 2px rgba(0, 0, 0, 0.9)';
		}
	}

	// ── Renderização ─────────────────────────────────────────────────────────
	function render(items) {
		overlay.innerHTML = '';
		panel.innerHTML = '';
		const usePanel = panelModeInput.checked;
		panel.style.display = usePanel ? 'flex' : 'none';
		if (!items || !items.length) return;
		if (usePanel) renderPanel(items);
		else renderCanvas(items);
	}

	function renderCanvas(items) {
		const W = overlay.clientWidth || 1280;
		const H = overlay.clientHeight || 720;
		const bg = hexToRgba(theme().bg, parseFloat(bgOpacityInput.value));
		for (const it of items) {
			const x = norm(it.x), y = norm(it.y);
			const w = Math.max(0.02, norm(it.w)), h = Math.max(0.02, norm(it.h));
			const boxW = Math.max(24, w * W);
			const boxH = Math.max(16, h * H);
			const div = document.createElement('div');
			div.className = 'trans-block';
			div.style.left = (x * 100) + '%';
			div.style.top = (y * 100) + '%';
			div.style.width = boxW + 'px';
			div.style.minHeight = boxH + 'px';
			div.style.background = bg;
			div.textContent = it.pt;
			styleText(div);
			overlay.appendChild(div);
			fitText(div, boxW, boxH);
		}
	}

	// Reduz a fonte até o texto caber na caixa (largura + altura com folga).
	// A escala (slider) multiplica o tamanho inicial; o loop garante que caiba.
	function fitText(el, boxW, boxH) {
		const scale = parseFloat(fontScaleInput.value) || 0.8;
		const maxH = boxH * 1.4; // deixa crescer um pouco: PT costuma ser mais longo
		let fs = Math.max(8, Math.min(34, boxH * 0.6) * scale);
		el.style.fontSize = fs + 'px';
		let guard = 60;
		while (fs > 8 && guard-- > 0 && (el.scrollWidth > boxW + 1 || el.scrollHeight > maxH + 1)) {
			fs -= 1;
			el.style.fontSize = fs + 'px';
		}
	}

	function renderPanel(items) {
		const scale = parseFloat(fontScaleInput.value) || 0.8;
		for (const it of items) {
			const item = document.createElement('div');
			item.className = 'panel-item';
			item.style.fontFamily = fontInput.value;
			if (it.original) {
				const src = document.createElement('span');
				src.className = 'panel-src';
				src.style.fontSize = (13 * scale) + 'px';
				src.textContent = it.original;
				item.appendChild(src);
			}
			const pt = document.createElement('span');
			pt.className = 'panel-pt';
			pt.style.fontSize = (22 * scale) + 'px';
			pt.textContent = it.pt;
			styleText(pt);
			item.appendChild(pt);
			panel.appendChild(item);
		}
	}

	function showTranslation() {
		visible = true;
		render(lastItems);
	}

	// Esconde da tela, mas MANTÉM lastItems (dá pra reespiar com "Segurar para ver").
	function hideTranslation() {
		visible = false;
		overlay.innerHTML = '';
		panel.innerHTML = '';
		panel.style.display = 'none';
	}

	// "Esconder": tira da tela, mas MANTÉM lastItems na memória (peek/cache reexibem).
	function clearTranslation() {
		clearTimeout(hideTimer);
		peeking = false;
		hideTranslation();
		setStatus('');
	}

	function scheduleHide() {
		clearTimeout(hideTimer);
		const secs = parseInt(hideAfterInput.value, 10) || 0;
		if (secs > 0) hideTimer = setTimeout(hideTranslation, secs * 1000);
	}

	// Toggle (tecla E): esconde se estiver visível, mostra a última se estiver oculta.
	function toggleTranslation() {
		if (visible) {
			clearTranslation();
		} else {
			if (!lastItems.length) { setStatus('Nada traduzido ainda.', true); return; }
			clearTimeout(hideTimer);
			showTranslation();
		}
	}

	// Segurar (botão ou tecla R) mostra a última tradução; soltar restaura o estado.
	function startPeek(e) {
		if (e) e.preventDefault();
		if (peeking) return;
		if (!lastItems.length) { setStatus('Nada traduzido ainda.', true); return; }
		peeking = true;
		wasVisible = visible;
		clearTimeout(hideTimer);
		showTranslation();
	}

	function endPeek() {
		if (!peeking) return;
		peeking = false;
		if (!wasVisible) hideTranslation();
	}

	// ── Fluxo principal ──────────────────────────────────────────────────────
	async function translateScreen() {
		if (busy) return;
		const apiKey = keyInput.value.trim();
		if (!apiKey) { setStatus('Cole a chave da API do Gemini primeiro.', true); return; }

		// Cache de frames: se a tela repete, reusa sem chamar a API (custo zero).
		const grid = frameFingerprint();
		const hit = cacheEnabledInput.checked ? cacheLookup(grid) : null;
		if (hit) {
			lastItems = hit.items;
			showTranslation();
			setStatus(lastItems.length ? 'Reaproveitado (sem custo).' : 'Nenhum texto detectado.');
			recordHit();
			scheduleHide();
			return;
		}

		const frame = captureFrame();
		if (!frame) { setStatus('Sem vídeo pra capturar.', true); return; }

		busy = true;
		setStatus('Traduzindo…');
		indicator.style.display = 'flex';
		try {
			const { items, usage } = await callGemini(frame, apiKey);
			lastItems = (Array.isArray(items) ? items : []).filter(it => isMeaningful(it.pt));
			showTranslation();
			setStatus(lastItems.length ? `${lastItems.length} bloco(s) traduzido(s).` : 'Nenhum texto detectado.');
			if (cacheEnabledInput.checked) cacheStore(grid, lastItems);
			recordCall(usage);
			scheduleHide();
		} catch (err) {
			console.error(err);
			setStatus('Erro: ' + err.message, true);
		} finally {
			busy = false;
			indicator.style.display = 'none';
		}
	}

	// ── Disparos: botões (sem worker automático) ─────────────────────────────
	translateBtn.addEventListener('click', translateScreen);
	clearBtn.addEventListener('click', clearTranslation);

	// "Segurar para ver": mostra enquanto pressionado, esconde ao soltar.
	peekBtn.addEventListener('mousedown', startPeek);
	peekBtn.addEventListener('touchstart', startPeek, { passive: false });
	document.addEventListener('mouseup', endPeek);
	peekBtn.addEventListener('touchend', endPeek);
	peekBtn.addEventListener('touchcancel', endPeek);
	// Evita que o clique conte como "arrastar seleção" e trave o peek.
	peekBtn.addEventListener('dragstart', (e) => e.preventDefault());

	// ── Atalhos de teclado: T traduzir, R reexibir (segurar), E esconder ─────
	function typingInField(e) {
		const tag = (e.target.tagName || '').toLowerCase();
		return tag === 'input' || tag === 'select' || tag === 'textarea';
	}

	document.addEventListener('keydown', (e) => {
		if (!enabledInput.checked || typingInField(e) || e.repeat && e.key.toLowerCase() !== 'r') return;
		const k = e.key.toLowerCase();
		if (k === 't') { e.preventDefault(); translateScreen(); }
		else if (k === 'e') { e.preventDefault(); toggleTranslation(); }
		else if (k === 'r') { e.preventDefault(); startPeek(); }
	});

	document.addEventListener('keyup', (e) => {
		if (e.key.toLowerCase() === 'r') endPeek();
	});

	// Reposiciona o texto ao redimensionar (fontes do overlay dependem da altura).
	window.addEventListener('resize', rerender);

	// Atualiza as janelas de 60s (RPM/TPM) enquanto a sub-aba Uso está aberta.
	setInterval(() => {
		if (sidebar.style.display !== 'none' && usoPanel && usoPanel.classList.contains('active')) refreshUsage();
	}, 1000);

	loadPrefs();
})();
