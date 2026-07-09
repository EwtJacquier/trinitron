// ── Trinitron: tradução de tela em tempo real via Google Gemini ──────────────
// Captura o frame limpo do <video>, pede OCR + tradução (PT-BR) + posições ao
// Gemini numa chamada só, e desenha um overlay configurável sobre o canvas.

(function () {
	const DEFAULT_MODEL = 'gemini-flash-latest'; // alias sempre atual → não quebra em deprecações
	const MAX_CAPTURE_WIDTH = 1280; // reduz payload/custo sem perder legibilidade
	const LS = {
		key: 'trinitron.geminiKey',
		model: 'trinitron.geminiModel',
		enabled: 'trinitron.translateEnabled',
		fontColor: 'trinitron.transFontColor',
		bgColor: 'trinitron.transBgColor',
		bgOpacity: 'trinitron.transBgOpacity',
		auto: 'trinitron.autoTranslate',
		sensitivity: 'trinitron.autoSensitivity',
		interval: 'trinitron.autoInterval'
	};

	const video = document.getElementById('video');
	const overlay = document.getElementById('translationOverlay');
	const keyInput = document.getElementById('geminiKey');
	const modelInput = document.getElementById('geminiModel');
	const enabledInput = document.getElementById('translateEnabled');
	const translateBtn = document.getElementById('translateBtn');
	const clearBtn = document.getElementById('translateClearBtn');
	const fontColorInput = document.getElementById('transFontColor');
	const bgColorInput = document.getElementById('transBgColor');
	const bgOpacityInput = document.getElementById('transBgOpacity');
	const autoInput = document.getElementById('autoTranslate');
	const sensitivityInput = document.getElementById('autoSensitivity');
	const intervalInput = document.getElementById('autoInterval');
	const intervalValEl = document.getElementById('autoIntervalVal');
	const statusEl = document.getElementById('translateStatus');

	let busy = false;
	let lastItems = [];

	// ── Persistência simples no navegador ────────────────────────────────────
	function loadPrefs() {
		keyInput.value = localStorage.getItem(LS.key) || '';
		modelInput.value = localStorage.getItem(LS.model) || DEFAULT_MODEL;
		enabledInput.checked = localStorage.getItem(LS.enabled) === 'true';
		fontColorInput.value = localStorage.getItem(LS.fontColor) || '#ffffff';
		bgColorInput.value = localStorage.getItem(LS.bgColor) || '#000000';
		bgOpacityInput.value = localStorage.getItem(LS.bgOpacity) || '0.75';
		autoInput.checked = localStorage.getItem(LS.auto) === 'true';
		sensitivityInput.value = localStorage.getItem(LS.sensitivity) || '0.5';
		intervalInput.value = localStorage.getItem(LS.interval) || '4';
		intervalValEl.textContent = intervalInput.value;
	}

	keyInput.addEventListener('change', () => localStorage.setItem(LS.key, keyInput.value.trim()));
	modelInput.addEventListener('change', () => localStorage.setItem(LS.model, modelInput.value.trim()));
	enabledInput.addEventListener('change', () => localStorage.setItem(LS.enabled, enabledInput.checked));
	fontColorInput.addEventListener('input', () => { localStorage.setItem(LS.fontColor, fontColorInput.value); renderOverlay(lastItems); });
	bgColorInput.addEventListener('input', () => { localStorage.setItem(LS.bgColor, bgColorInput.value); renderOverlay(lastItems); });
	bgOpacityInput.addEventListener('input', () => { localStorage.setItem(LS.bgOpacity, bgOpacityInput.value); renderOverlay(lastItems); });
	autoInput.addEventListener('change', () => { localStorage.setItem(LS.auto, autoInput.checked); resetDetector(); });
	sensitivityInput.addEventListener('input', () => localStorage.setItem(LS.sensitivity, sensitivityInput.value));
	intervalInput.addEventListener('input', () => { localStorage.setItem(LS.interval, intervalInput.value); intervalValEl.textContent = intervalInput.value; });

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

	// ── Desenha o overlay ────────────────────────────────────────────────────
	function renderOverlay(items) {
		overlay.innerHTML = '';
		if (!items || !items.length) return;
		const H = overlay.clientHeight || 720;
		const fontColor = fontColorInput.value;
		const bg = hexToRgba(bgColorInput.value, parseFloat(bgOpacityInput.value));
		for (const it of items) {
			const x = norm(it.x), y = norm(it.y);
			const w = Math.max(0.02, norm(it.w)), h = Math.max(0.02, norm(it.h));
			const div = document.createElement('div');
			div.className = 'trans-block';
			div.style.left = (x * 100) + '%';
			div.style.top = (y * 100) + '%';
			div.style.width = (w * 100) + '%';
			div.style.minHeight = (h * 100) + '%';
			div.style.color = fontColor;
			div.style.background = bg;
			div.style.fontSize = Math.max(11, Math.min(48, h * H * 0.7)) + 'px';
			div.textContent = it.pt;
			overlay.appendChild(div);
		}
	}

	function clearOverlay() {
		lastItems = [];
		overlay.innerHTML = '';
		setStatus('');
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
			renderOverlay(lastItems);
			setStatus(lastItems.length ? `${lastItems.length} bloco(s) traduzido(s).` : 'Nenhum texto detectado.');
		} catch (err) {
			console.error(err);
			setStatus('Erro: ' + err.message, true);
		} finally {
			busy = false;
		}
	}

	// ── Detector heurístico local (sem IA) ───────────────────────────────────
	// Trabalha numa versão minúscula do frame em tons de cinza. Decide QUANDO
	// disparar o Gemini: espera a cena mudar e depois ESTABILIZAR (caixa de
	// diálogo terminou de escrever) e só então traduz — uma vez por conteúdo.
	const DET_W = 192, DET_H = 108;   // resolução de análise
	const GRID_X = 24, GRID_Y = 14;   // fingerprint grosseiro p/ detectar mudança
	const SAMPLE_MS = 350;            // frequência de amostragem
	const REQUIRED_STABLE = 2;        // amostras estáveis antes de disparar
	const CHANGE_THRESH = 6;          // diff média por célula (0–255) = "mudou"

	let detCanvas = null, detCtx = null;
	let prevGrid = null;              // fingerprint da amostra anterior
	let translatedGrid = null;        // fingerprint do que já foi traduzido
	let stableCount = 0;
	let dirty = false;                // há conteúdo novo ainda não traduzido
	let lastAutoCall = 0;

	function resetDetector() {
		prevGrid = null;
		translatedGrid = null;
		stableCount = 0;
		dirty = false;
	}

	// Extrai o fingerprint (grade de luminância) e um "textScore" (densidade de
	// bordas horizontais — texto gera muitas transições nítidas e pequenas).
	function analyzeFrame() {
		if (!detCanvas) {
			detCanvas = document.createElement('canvas');
			detCanvas.width = DET_W; detCanvas.height = DET_H;
			detCtx = detCanvas.getContext('2d', { willReadFrequently: true });
		}
		detCtx.drawImage(video, 0, 0, DET_W, DET_H);
		const data = detCtx.getImageData(0, 0, DET_W, DET_H).data;

		const gray = new Uint8Array(DET_W * DET_H);
		for (let i = 0, p = 0; i < data.length; i += 4, p++) {
			gray[p] = (data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114) | 0;
		}

		// Grade de luminância média por célula.
		const grid = new Float32Array(GRID_X * GRID_Y);
		const counts = new Int32Array(GRID_X * GRID_Y);
		for (let y = 0; y < DET_H; y++) {
			const gy = (y * GRID_Y / DET_H) | 0;
			for (let x = 0; x < DET_W; x++) {
				const gx = (x * GRID_X / DET_W) | 0;
				const c = gy * GRID_X + gx;
				grid[c] += gray[y * DET_W + x];
				counts[c]++;
			}
		}
		for (let c = 0; c < grid.length; c++) grid[c] /= counts[c] || 1;

		// textScore: fração de pixels com gradiente horizontal forte.
		let strong = 0;
		for (let y = 0; y < DET_H; y++) {
			for (let x = 1; x < DET_W; x++) {
				const d = Math.abs(gray[y * DET_W + x] - gray[y * DET_W + x - 1]);
				if (d > 40) strong++;
			}
		}
		const textScore = strong / (DET_W * DET_H);
		return { grid, textScore };
	}

	function gridDiff(a, b) {
		if (!a || !b) return Infinity;
		let sum = 0;
		for (let i = 0; i < a.length; i++) sum += Math.abs(a[i] - b[i]);
		return sum / a.length;
	}

	function detectorTick() {
		if (!autoInput.checked || busy) return;
		if (video.readyState < 2 || !video.videoWidth) return;

		const { grid, textScore } = analyzeFrame();
		const changed = gridDiff(grid, prevGrid) > CHANGE_THRESH;
		prevGrid = grid;

		if (changed) {           // cena mudando → espera estabilizar
			stableCount = 0;
			dirty = true;
			return;
		}
		if (!dirty) return;      // cena estática já tratada

		// Sensibilidade (0–1) → limiar de textScore (alto = dispara mais fácil).
		const threshold = (1 - parseFloat(sensitivityInput.value)) * 0.22 + 0.015;
		if (textScore < threshold) return;

		// Mesmo conteúdo já traduzido? não redispara.
		if (translatedGrid && gridDiff(grid, translatedGrid) < CHANGE_THRESH) {
			dirty = false;
			return;
		}

		if (++stableCount < REQUIRED_STABLE) return;

		const now = performance.now();
		if (now - lastAutoCall < parseFloat(intervalInput.value) * 1000) return;

		lastAutoCall = now;
		translatedGrid = grid;
		dirty = false;
		stableCount = 0;
		translateScreen();
	}

	setInterval(detectorTick, SAMPLE_MS);

	// ── Disparos: botão + tecla T ────────────────────────────────────────────
	translateBtn.addEventListener('click', translateScreen);
	clearBtn.addEventListener('click', clearOverlay);

	document.addEventListener('keydown', (e) => {
		const tag = (e.target.tagName || '').toLowerCase();
		if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
		if (enabledInput.checked && (e.key === 't' || e.key === 'T')) {
			e.preventDefault();
			translateScreen();
		}
	});

	// Reposiciona o texto ao redimensionar (fontes dependem da altura do overlay).
	window.addEventListener('resize', () => renderOverlay(lastItems));

	loadPrefs();
})();
