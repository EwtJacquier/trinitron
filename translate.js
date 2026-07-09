// ── Trinitron: tradução de tela via Google Gemini ────────────────────────────
// Captura o frame limpo do <video>, pede OCR + tradução (PT-BR) + posições ao
// Gemini numa chamada só, e mostra o resultado como overlay sobre o canvas ou
// num painel lateral. Disparo manual apenas: botão ou tecla T.

(function () {
	const DEFAULT_MODEL = 'gemini-2.5-flash-lite'; // barato e rápido p/ OCR+tradução
	const MAX_CAPTURE_WIDTH = 1280; // reduz payload/custo sem perder legibilidade

	// Fingerprint de frame (cache de reuso) — mesma heurística de grade da Fase 2.
	const DET_W = 192, DET_H = 108;   // resolução de análise
	const GRID_X = 24, GRID_Y = 14;   // grade grosseira de luminância
	const CACHE_MAX = 12;             // entradas guardadas por sessão
	const CACHE_THRESH = 4;           // diff média por célula p/ considerar "mesma tela"
	const LS = {
		key: 'trinitron.geminiKey',
		model: 'trinitron.geminiModel',
		enabled: 'trinitron.translateEnabled',
		panelMode: 'trinitron.transPanelMode',
		font: 'trinitron.transFont',
		preset: 'trinitron.transPreset',
		fontScale: 'trinitron.transFontScale',
		bgOpacity: 'trinitron.transBgOpacity',
		borderWidth: 'trinitron.transBorderWidth',
		hideAfter: 'trinitron.transHideAfter'
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

	let busy = false;
	let lastItems = [];
	let hideTimer = null;
	let visible = false;   // tradução atualmente na tela?
	let peeking = false;   // botão "segurar para ver" pressionado?
	let wasVisible = false;
	let detCanvas = null, detCtx = null; // canvas minúsculo p/ fingerprint
	const frameCache = [];               // LRU: { grid, items }

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
	}

	function save(key, val) { localStorage.setItem(key, val); }
	function rerender() { if (visible) render(lastItems); }

	keyInput.addEventListener('change', () => save(LS.key, keyInput.value.trim()));
	modelInput.addEventListener('change', () => save(LS.model, modelInput.value.trim()));
	enabledInput.addEventListener('change', () => save(LS.enabled, enabledInput.checked));
	panelModeInput.addEventListener('change', () => { save(LS.panelMode, panelModeInput.checked); rerender(); });
	fontInput.addEventListener('change', () => { save(LS.font, fontInput.value); rerender(); });
	presetInput.addEventListener('change', () => { save(LS.preset, presetInput.value); rerender(); });
	fontScaleInput.addEventListener('input', () => { save(LS.fontScale, fontScaleInput.value); fontScaleValEl.textContent = fontScaleInput.value; rerender(); });
	bgOpacityInput.addEventListener('input', () => { save(LS.bgOpacity, bgOpacityInput.value); rerender(); });
	borderWidthInput.addEventListener('input', () => { save(LS.borderWidth, borderWidthInput.value); borderValEl.textContent = borderWidthInput.value; rerender(); });
	hideAfterInput.addEventListener('input', () => { save(LS.hideAfter, hideAfterInput.value); hideValEl.textContent = hideAfterInput.value; });

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

	function gridDiff(a, b) {
		if (!a || !b) return Infinity;
		let sum = 0;
		for (let i = 0; i < a.length; i++) sum += Math.abs(a[i] - b[i]);
		return sum / a.length;
	}

	// Procura no cache uma tela parecida; devolve a entrada (e a promove no LRU).
	function cacheLookup(grid) {
		if (!grid) return null;
		for (let i = 0; i < frameCache.length; i++) {
			if (gridDiff(grid, frameCache[i].grid) < CACHE_THRESH) {
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

	// ── Chamada ao Gemini com saída estruturada (JSON) ───────────────────────
	const PROMPT =
		'Você é um tradutor de tela de videogame em tempo real. Analise a imagem e ' +
		'detecte TODOS os blocos de texto legível (japonês ou inglês). Para cada bloco forneça: ' +
		'"original" (texto detectado), "pt" (tradução natural para português do Brasil no contexto de um jogo), ' +
		'e a caixa delimitadora como frações da imagem entre 0 e 1: "x","y" (canto superior esquerdo), ' +
		'"w","h" (largura e altura). Ignore texto decorativo ilegível, logos e marcas d\'água. ' +
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
		return JSON.parse(text);
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
		const hit = cacheLookup(grid);
		if (hit) {
			lastItems = hit.items;
			showTranslation();
			setStatus(lastItems.length ? 'Reaproveitado (sem custo).' : 'Nenhum texto detectado.');
			scheduleHide();
			return;
		}

		const frame = captureFrame();
		if (!frame) { setStatus('Sem vídeo pra capturar.', true); return; }

		busy = true;
		setStatus('Traduzindo…');
		indicator.style.display = 'flex';
		try {
			const items = await callGemini(frame, apiKey);
			lastItems = Array.isArray(items) ? items : [];
			showTranslation();
			setStatus(lastItems.length ? `${lastItems.length} bloco(s) traduzido(s).` : 'Nenhum texto detectado.');
			cacheStore(grid, lastItems);
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

	loadPrefs();
})();
