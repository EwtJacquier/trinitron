// Ferramentas de desenvolvimento pra comparar filtros no console (nao alteram o render normal).
// Todas usam a textura de video ja carregada, entao "com" e "sem" filtro sao do MESMO frame.
//
//   cmpGrab('revive')                     -> canvas 2D 1920x1080 com o frame renderizado por esse filtro
//   cmpDiff('original', 'sharpen', 4)     -> overlay preto/branco de onde o filtro mexeu (ganho 4x) + stats
//   cmpPair(x, y, w, h, scale)            -> overlay: recorte ampliado, em cima A (sem), embaixo B (com)
//   cmpRegion(x, y, w, h)                 -> { mean, max, changedPct } da diferenca A vs B numa regiao
//   cmpHide()                             -> remove o overlay
//   cmpImage('prints/image3.png', 'revive', 8) -> roda um filtro single-texture num print e mostra lado a lado
// cmpDiff guarda o par em window.cmpA / window.cmpB, usados por cmpPair e cmpRegion.
// So funciona com filtros full-res (canvas 1920x1080).

(function () {
	const W = 1920, H = 1080;

	function overlay() {
		let ov = document.getElementById('cmpOverlay');
		if (!ov) {
			ov = document.createElement('canvas');
			ov.id = 'cmpOverlay';
			ov.style.cssText = 'position:fixed;left:0;top:0;z-index:1000;';
			document.body.appendChild(ov);
		}
		return ov;
	}

	window.cmpGrab = function (name) {
		const prev = filter.value;
		filter.value = name;
		recompileProgram();
		gl.viewport(0, 0, canvas.width, canvas.height);
		gl.bindTexture(gl.TEXTURE_2D, texture);
		gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
		gl.clearColor(0, 0, 0, 1);
		gl.clear(gl.COLOR_BUFFER_BIT);
		gl.drawArrays(gl.TRIANGLES, 0, 6);
		const o = document.createElement('canvas');
		o.width = W; o.height = H;
		o.getContext('2d').drawImage(canvas, 0, 0);
		filter.value = prev;
		recompileProgram();
		return o;
	};

	window.cmpDiff = function (aName, bName, gain) {
		gain = gain || 4;
		gl.bindTexture(gl.TEXTURE_2D, texture);
		gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
		const A = cmpGrab(aName), B = cmpGrab(bName);
		const da = A.getContext('2d').getImageData(0, 0, W, H).data;
		const db = B.getContext('2d').getImageData(0, 0, W, H).data;
		const out = new ImageData(W, H), d = out.data;
		let changed = 0, sum = 0;
		for (let i = 0; i < da.length; i += 4) {
			const m = Math.max(Math.abs(da[i] - db[i]), Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2]));
			if (m > 6) changed++;
			sum += m;
			const v = Math.min(255, m * gain);
			d[i] = v; d[i + 1] = v; d[i + 2] = v; d[i + 3] = 255;
		}
		const ov = overlay();
		ov.width = W; ov.height = H;
		ov.style.width = Math.min(innerWidth, 1568) + 'px'; ov.style.height = '';
		ov.getContext('2d').putImageData(out, 0, 0);
		window.cmpA = A; window.cmpB = B;
		return { changedPct: (100 * changed / (W * H)).toFixed(2), meanDiff: (sum / (W * H)).toFixed(3) };
	};

	window.cmpPair = function (x, y, w, h, scale) {
		const ov = overlay();
		ov.style.width = ''; ov.style.height = '';
		ov.width = w * scale; ov.height = h * scale * 2 + 4;
		const c = ov.getContext('2d');
		c.imageSmoothingEnabled = false;
		c.fillStyle = '#f0f'; c.fillRect(0, 0, ov.width, ov.height);
		c.drawImage(cmpA, x, y, w, h, 0, 0, w * scale, h * scale);
		c.drawImage(cmpB, x, y, w, h, 0, h * scale + 4, w * scale, h * scale);
		return ov.width + 'x' + ov.height;
	};

	window.cmpRegion = function (x, y, w, h) {
		const da = cmpA.getContext('2d').getImageData(x, y, w, h).data;
		const db = cmpB.getContext('2d').getImageData(x, y, w, h).data;
		let sum = 0, mx = 0, changed = 0;
		for (let i = 0; i < da.length; i += 4) {
			const m = Math.max(Math.abs(da[i] - db[i]), Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2]));
			sum += m; if (m > mx) mx = m; if (m > 6) changed++;
		}
		return { mean: (sum / (w * h)).toFixed(2), max: mx, changedPct: (100 * changed / (w * h)).toFixed(1) };
	};

	window.cmpHide = function () {
		const ov = document.getElementById('cmpOverlay');
		if (ov) ov.remove();
	};

	// Grade de luma (0-9) de uma regiao do ultimo par (A = sem filtro, B = com), lado a lado.
	// Marca com > os pixels que mudaram. Serve pra ver o que o filtro fez em numeros.
	window.cmpGrid = function (x0, y0, w, h) {
		const da = cmpA.getContext('2d').getImageData(x0, y0, w, h).data;
		const db = cmpB.getContext('2d').getImageData(x0, y0, w, h).data;
		const L = (d, i) => Math.round((0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]) / 25.5);
		const rows = [];
		for (let y = 0; y < h; y++) {
			const r = [];
			for (let x = 0; x < w; x++) {
				const i = (y * w + x) * 4, a = L(da, i), b = L(db, i);
				r.push(a + (a !== b ? '>' + b : '  '));
			}
			rows.push(String(y0 + y).padStart(4) + ': ' + r.join(' '));
		}
		return rows.join('\n');
	};

	// Pior pixel (maior diferenca A vs B) de uma regiao, com a grade de luma em volta
	window.cmpWorst = function (x0, y0, w, h, rad) {
		rad = rad || 4;
		const da = cmpA.getContext('2d').getImageData(x0, y0, w, h).data;
		const db = cmpB.getContext('2d').getImageData(x0, y0, w, h).data;
		let best = { m: 0, x: x0, y: y0 };
		for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
			const i = (y * w + x) * 4;
			const m = Math.max(Math.abs(da[i] - db[i]), Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2]));
			if (m > best.m) best = { m, x: x0 + x, y: y0 + y };
		}
		return { best, grid: cmpGrid(best.x - rad, best.y - rad, rad * 2 + 1, rad * 2 + 1) };
	};

	// Roda um filtro sobre uma imagem (ex. um print em prints/) na resolucao nativa dela e mostra
	// lado a lado ampliado: esquerda original, direita filtrado. Retorna quantos pixels mudaram.
	window.cmpImage = async function (url, name, scale) {
		// onload em vez de decode(): decode() nao resolve com a aba em segundo plano
		const img = new Image();
		await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url + '?' + Date.now(); });
		const w = img.width, h = img.height;
		const prev = filter.value;
		filter.value = name;
		recompileProgram();
		gl.uniform2f(gl.getUniformLocation(program, 'u_textureSize'), w, h);
		gl.bindTexture(gl.TEXTURE_2D, texture);
		// NEAREST pra cada amostra do shader cair exatamente num texel da imagem
		gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
		gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
		gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
		gl.viewport(0, 0, canvas.width, canvas.height);
		gl.clear(gl.COLOR_BUFFER_BIT);
		gl.drawArrays(gl.TRIANGLES, 0, 6);
		const out = document.createElement('canvas');
		out.width = w; out.height = h;
		const oc = out.getContext('2d');
		oc.imageSmoothingEnabled = false;
		oc.drawImage(canvas, 0, 0, canvas.width, canvas.height, 0, 0, w, h);
		gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
		gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
		filter.value = prev;
		recompileProgram();

		const ov = overlay();
		ov.style.width = ''; ov.style.height = '';
		ov.width = w * scale * 2 + 4; ov.height = h * scale;
		const c = ov.getContext('2d');
		c.imageSmoothingEnabled = false;
		c.fillStyle = '#f0f'; c.fillRect(0, 0, ov.width, ov.height);
		c.drawImage(img, 0, 0, w * scale, h * scale);
		c.drawImage(out, w * scale + 4, 0, w * scale, h * scale);

		const a = document.createElement('canvas');
		a.width = w; a.height = h;
		a.getContext('2d').drawImage(img, 0, 0);
		const da = a.getContext('2d').getImageData(0, 0, w, h).data, db = oc.getImageData(0, 0, w, h).data;
		let changed = 0;
		for (let i = 0; i < da.length; i += 4) {
			if (Math.max(Math.abs(da[i] - db[i]), Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2])) > 6) changed++;
		}
		window.cmpA = a; window.cmpB = out;   // pra cmpGrid / cmpWorst / cmpPair no print
		return { size: w + 'x' + h, changed };
	};
})();
