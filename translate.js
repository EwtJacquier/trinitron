// ── Trinitron: tradução de tela em tempo real via Google Gemini ──────────────
// Captura o frame limpo do <video>, pede OCR + tradução (PT-BR) + posições ao
// Gemini numa chamada só, e desenha um overlay configurável sobre o canvas.

(function () {
	const GEMINI_MODEL = 'gemini-2.0-flash';
	const MAX_CAPTURE_WIDTH = 1280; // reduz payload/custo sem perder legibilidade
	const LS = {
		key: 'trinitron.geminiKey',
		enabled: 'trinitron.translateEnabled',
		fontColor: 'trinitron.transFontColor',
		bgColor: 'trinitron.transBgColor',
		bgOpacity: 'trinitron.transBgOpacity'
	};

	const video = document.getElementById('video');
	const overlay = document.getElementById('translationOverlay');
	const keyInput = document.getElementById('geminiKey');
	const enabledInput = document.getElementById('translateEnabled');
	const translateBtn = document.getElementById('translateBtn');
	const clearBtn = document.getElementById('translateClearBtn');
	const fontColorInput = document.getElementById('transFontColor');
	const bgColorInput = document.getElementById('transBgColor');
	const bgOpacityInput = document.getElementById('transBgOpacity');
	const statusEl = document.getElementById('translateStatus');

	let busy = false;
	let lastItems = [];

	// ── Persistência simples no navegador ────────────────────────────────────
	function loadPrefs() {
		keyInput.value = localStorage.getItem(LS.key) || '';
		enabledInput.checked = localStorage.getItem(LS.enabled) === 'true';
		fontColorInput.value = localStorage.getItem(LS.fontColor) || '#ffffff';
		bgColorInput.value = localStorage.getItem(LS.bgColor) || '#000000';
		bgOpacityInput.value = localStorage.getItem(LS.bgOpacity) || '0.75';
	}

	keyInput.addEventListener('change', () => localStorage.setItem(LS.key, keyInput.value.trim()));
	enabledInput.addEventListener('change', () => localStorage.setItem(LS.enabled, enabledInput.checked));
	fontColorInput.addEventListener('input', () => { localStorage.setItem(LS.fontColor, fontColorInput.value); renderOverlay(lastItems); });
	bgColorInput.addEventListener('input', () => { localStorage.setItem(LS.bgColor, bgColorInput.value); renderOverlay(lastItems); });
	bgOpacityInput.addEventListener('input', () => { localStorage.setItem(LS.bgOpacity, bgOpacityInput.value); renderOverlay(lastItems); });

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
		const url = `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent?key=${encodeURIComponent(apiKey)}`;
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
		setStatus('🌐 Traduzindo…');
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
