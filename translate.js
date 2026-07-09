// ── Trinitron: tradução de tela via Google Gemini ────────────────────────────
// Captura o frame limpo do <video>, pede OCR + tradução (PT-BR) + posições ao
// Gemini numa chamada só, e mostra o resultado como overlay sobre o canvas ou
// num painel lateral. Disparo manual apenas: botão ou tecla T.

(function () {
	const DEFAULT_MODEL = 'gemini-flash-latest'; // alias sempre atual → não quebra em deprecações
	const MAX_CAPTURE_WIDTH = 1280; // reduz payload/custo sem perder legibilidade
	const LS = {
		key: 'trinitron.geminiKey',
		model: 'trinitron.geminiModel',
		enabled: 'trinitron.translateEnabled',
		panelMode: 'trinitron.transPanelMode',
		font: 'trinitron.transFont',
		preset: 'trinitron.transPreset',
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
	const clearBtn = document.getElementById('translateClearBtn');
	const panelModeInput = document.getElementById('transPanelMode');
	const fontInput = document.getElementById('transFont');
	const presetInput = document.getElementById('transPreset');
	const bgOpacityInput = document.getElementById('transBgOpacity');
	const borderWidthInput = document.getElementById('transBorderWidth');
	const borderValEl = document.getElementById('transBorderVal');
	const hideAfterInput = document.getElementById('transHideAfter');
	const hideValEl = document.getElementById('transHideVal');
	const statusEl = document.getElementById('translateStatus');

	let busy = false;
	let lastItems = [];
	let hideTimer = null;

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

	// ── Persistência simples no navegador ────────────────────────────────────
	function loadPrefs() {
		keyInput.value = localStorage.getItem(LS.key) || '';
		modelInput.value = localStorage.getItem(LS.model) || DEFAULT_MODEL;
		enabledInput.checked = localStorage.getItem(LS.enabled) === 'true';
		panelModeInput.checked = localStorage.getItem(LS.panelMode) === 'true';
		fontInput.value = localStorage.getItem(LS.font) || 'sans-serif';
		presetInput.value = localStorage.getItem(LS.preset) || 'classico';
		bgOpacityInput.value = localStorage.getItem(LS.bgOpacity) || '0.75';
		borderWidthInput.value = localStorage.getItem(LS.borderWidth) || '0';
		borderValEl.textContent = borderWidthInput.value;
		hideAfterInput.value = localStorage.getItem(LS.hideAfter) || '0';
		hideValEl.textContent = hideAfterInput.value;
	}

	function save(key, val) { localStorage.setItem(key, val); }
	function rerender() { render(lastItems); }

	keyInput.addEventListener('change', () => save(LS.key, keyInput.value.trim()));
	modelInput.addEventListener('change', () => save(LS.model, modelInput.value.trim()));
	enabledInput.addEventListener('change', () => save(LS.enabled, enabledInput.checked));
	panelModeInput.addEventListener('change', () => { save(LS.panelMode, panelModeInput.checked); rerender(); });
	fontInput.addEventListener('change', () => { save(LS.font, fontInput.value); rerender(); });
	presetInput.addEventListener('change', () => { save(LS.preset, presetInput.value); rerender(); });
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
	// Resolve o problema de fontes grandes transbordando o box após traduzir.
	function fitText(el, boxW, boxH) {
		const maxH = boxH * 1.8; // deixa crescer um pouco: PT costuma ser mais longo
		let fs = Math.min(46, Math.max(11, boxH * 0.85));
		el.style.fontSize = fs + 'px';
		let guard = 60;
		while (fs > 9 && guard-- > 0 && (el.scrollWidth > boxW + 1 || el.scrollHeight > maxH + 1)) {
			fs -= 1;
			el.style.fontSize = fs + 'px';
		}
	}

	function renderPanel(items) {
		for (const it of items) {
			const item = document.createElement('div');
			item.className = 'panel-item';
			item.style.fontFamily = fontInput.value;
			if (it.original) {
				const src = document.createElement('span');
				src.className = 'panel-src';
				src.textContent = it.original;
				item.appendChild(src);
			}
			const pt = document.createElement('span');
			pt.className = 'panel-pt';
			pt.textContent = it.pt;
			styleText(pt);
			item.appendChild(pt);
			panel.appendChild(item);
		}
	}

	function clearTranslation() {
		clearTimeout(hideTimer);
		lastItems = [];
		overlay.innerHTML = '';
		panel.innerHTML = '';
		panel.style.display = 'none';
		setStatus('');
	}

	function scheduleHide() {
		clearTimeout(hideTimer);
		const secs = parseInt(hideAfterInput.value, 10) || 0;
		if (secs > 0 && lastItems.length) hideTimer = setTimeout(clearTranslation, secs * 1000);
	}

	// ── Fluxo principal ──────────────────────────────────────────────────────
	async function translateScreen() {
		if (busy) return;
		const apiKey = keyInput.value.trim();
		if (!apiKey) { setStatus('Cole a chave da API do Gemini primeiro.', true); return; }
		const frame = captureFrame();
		if (!frame) { setStatus('Sem vídeo pra capturar.', true); return; }

		busy = true;
		setStatus('Traduzindo…');
		try {
			const items = await callGemini(frame, apiKey);
			lastItems = Array.isArray(items) ? items : [];
			render(lastItems);
			setStatus(lastItems.length ? `${lastItems.length} bloco(s) traduzido(s).` : 'Nenhum texto detectado.');
			scheduleHide();
		} catch (err) {
			console.error(err);
			setStatus('Erro: ' + err.message, true);
		} finally {
			busy = false;
		}
	}

	// ── Disparos: botão + tecla T (sem worker automático) ────────────────────
	translateBtn.addEventListener('click', translateScreen);
	clearBtn.addEventListener('click', clearTranslation);

	document.addEventListener('keydown', (e) => {
		const tag = (e.target.tagName || '').toLowerCase();
		if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
		if (enabledInput.checked && (e.key === 't' || e.key === 'T')) {
			e.preventDefault();
			translateScreen();
		}
	});

	// Reposiciona o texto ao redimensionar (fontes do overlay dependem da altura).
	window.addEventListener('resize', rerender);

	loadPrefs();
})();
