const cameraSelect = document.getElementById('cameraSelect');
const micSelect = document.getElementById('micSelect');
const aspect = document.getElementById('ratio');
const filter = document.getElementById('filter');
const startBtn = document.getElementById('startBtn');
const container = document.getElementById('container');
const menu = document.getElementById('menu');
const sidebar = document.getElementById('sidebar');
const sidebarToggle = document.getElementById('sidebarToggle');
const fullscreenBtn = document.getElementById('fullscreenBtn');
const fpsDiv = document.getElementById('fps');
const volumeSlider = document.getElementById('volume');
const video = document.getElementById('video');
const audio = document.getElementById('audio');
const canvas = document.getElementById('canvas');

// ── UI inactivity ────────────────────────────────────────────────────────────
let streamActive = false;
let uiVisible = true;
let hideTimerId = null;
let fpsReceived = false;

function hideUI() {
	uiVisible = false;
	sidebarToggle.style.display = 'none';
	fullscreenBtn.style.display = 'none';
	sidebar.style.display = 'none';
	fpsDiv.style.display = 'none';
	document.body.style.cursor = 'none';
}

function showUI() {
	if (!uiVisible) {
		uiVisible = true;
		sidebarToggle.style.display = 'block';
		fullscreenBtn.style.display = 'block';
		if (fpsReceived) fpsDiv.style.display = 'block';
		document.body.style.cursor = '';
	}
	clearTimeout(hideTimerId);
	hideTimerId = setTimeout(hideUI, 3000);
}

document.addEventListener('mousemove', () => {
	if (streamActive) showUI();
});

// ── Fullscreen ───────────────────────────────────────────────────────────────
fullscreenBtn.addEventListener('click', () => {
	if (!document.fullscreenElement) {
		document.documentElement.requestFullscreen();
	} else {
		document.exitFullscreen();
	}
});

let videoStream;
let audioStream;
let gl;
let program;
let positionBuffer;
let texCoordBuffer;
let texture;
let videoAspect;

// Object to store loaded shaders
const filters = {};

// Vertex shader - vsSource
let vsSource = null;

// Load shader from file
async function loadShader(name) {
	const response = await fetch(`shaders/${name}.glsl`);
	return await response.text();
}

// Load all shaders
async function loadAllShaders() {
	const shaderNames = [
		'vertex',
		'original',
		'downscale',
		'crt',
		'crtgrainy',
		'blurrycrt',
		'blurrycrt2',
		'blurrygrainycrt',
		'sharpen',
		'grainy'
	];

	for (const name of shaderNames) {
		if (name === 'vertex') {
			vsSource = await loadShader(name);
		} else {
			filters[name] = await loadShader(name);
		}
	}
}

function createShader(gl, type, source) {
	const shader = gl.createShader(type);
	gl.shaderSource(shader, source);
	gl.compileShader(shader);
	if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
		console.error('Shader compile failed:', gl.getShaderInfoLog(shader));
		gl.deleteShader(shader);
		return null;
	}
	return shader;
}

function createProgram(gl, vsSource, fsSource) {
	fsSource = filters[filter.value];
	const vertexShader = createShader(gl, gl.VERTEX_SHADER, vsSource);
	const fragmentShader = createShader(gl, gl.FRAGMENT_SHADER, fsSource);
	const program = gl.createProgram();
	gl.attachShader(program, vertexShader);
	gl.attachShader(program, fragmentShader);
	gl.linkProgram(program);
	if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
		console.error('Program link failed:', gl.getProgramInfoLog(program));
		gl.deleteProgram(program);
		return null;
	}
	return program;
}

function recompileProgram() {
	if (!gl) return;
	if (program) gl.deleteProgram(program);

	program = createProgram(gl, vsSource, null);
	gl.useProgram(program);

	const positionLocation = gl.getAttribLocation(program, "a_position");
	const texCoordLocation = gl.getAttribLocation(program, "a_texCoord");

	gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
	gl.enableVertexAttribArray(positionLocation);
	gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 0, 0);

	gl.bindBuffer(gl.ARRAY_BUFFER, texCoordBuffer);
	gl.enableVertexAttribArray(texCoordLocation);
	gl.vertexAttribPointer(texCoordLocation, 2, gl.FLOAT, false, 0, 0);

	gl.uniform2f(gl.getUniformLocation(program, "u_textureSize"), filter.value === 'original' ? 1920 : 640.0, filter.value === 'original' ? 1080 : 360.0);
	gl.uniform2f(gl.getUniformLocation(program, "u_resolution"), canvas.width, canvas.height);

	updatePositionsForAspectRatio();
}

async function requestPermissionsAndListDevices() {
	try {
		const tempStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
		await listDevices();
		tempStream.getTracks().forEach(t => t.stop());
	} catch (err) {
		alert("Permission denied or error accessing media: " + err.message);
		console.error(err);
	}
}

async function listDevices() {
	const devices = await navigator.mediaDevices.enumerateDevices();
	cameraSelect.innerHTML = '';
	micSelect.innerHTML = '';

	devices.forEach(device => {
		const option = document.createElement('option');
		option.value = device.deviceId;
		option.text = device.label || `${device.kind}`;
		if (device.kind === 'videoinput') {
			cameraSelect.appendChild(option);
		} else if (device.kind === 'audioinput') {
			micSelect.appendChild(option);
		}
	});
}

async function startStream() {
	const videoSource = cameraSelect.value;
	const audioSource = micSelect.value;
	const ratio = aspect.value;

	container.classList.add(ratio);
	container.classList.add(filter.value);

	const videoConstraints = {
		deviceId: videoSource ? { exact: videoSource } : undefined,
		frameRate: { ideal: 60, max: 60 },
		width: { ideal: 1920 },
		height: { ideal: 1080 }
	};

	const audioConstraints = {
		deviceId: audioSource ? { exact: audioSource } : undefined,
		autoGainControl: false,
		echoCancellation: false,
		googAutoGainControl: false,
		noiseSuppression: false
	};

	try {
		if (videoStream) videoStream.getTracks().forEach(t => t.stop());
		if (audioStream) audioStream.getTracks().forEach(t => t.stop());

		videoStream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints });
		audioStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });

		video.srcObject = videoStream;
		audio.srcObject = audioStream;

		video.onloadedmetadata = () => {
			video.play();
			menu.style.display = 'none';
			sidebar.style.display = 'block';
			sidebarToggle.style.display = 'block';
			fullscreenBtn.style.display = 'block';
			streamActive = true;
			hideTimerId = setTimeout(hideUI, 3000);
			initWebGL();
			startRenderLoop();
		};
	} catch (err) {
		alert("Error starting media: " + err.message);
		console.error(err);
	}
}

function initWebGL() {
	gl = canvas.getContext('webgl');
	if (!gl) {
		alert('WebGL not supported');
		return;
	}

	positionBuffer = gl.createBuffer();
	gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
	setPositionFullScreen();

	texCoordBuffer = gl.createBuffer();
	gl.bindBuffer(gl.ARRAY_BUFFER, texCoordBuffer);
	gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
		0, 0,
		1, 0,
		0, 1,
		0, 1,
		1, 0,
		1, 1,
	]), gl.STATIC_DRAW);

	texture = gl.createTexture();
	gl.bindTexture(gl.TEXTURE_2D, texture);

	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

	videoAspect = video.videoWidth / video.videoHeight;

	window.addEventListener('resize', () => {
		updatePositionsForAspectRatio();
	});

	recompileProgram();
}

function setPositionFullScreen() {
	const fullScreenCoords = new Float32Array([
		-1, 1,
		1, 1,
		-1, -1,
		-1, -1,
		1, 1,
		1, -1,
	]);
	gl.bufferData(gl.ARRAY_BUFFER, fullScreenCoords, gl.STATIC_DRAW);
}

function updatePositionsForAspectRatio() {
	if (!gl) return;

	// Atualizar os buffers de posição e coordenadas de textura
	// para cobrir todo o canvas
	const positions = new Float32Array([
		-1, 1,
		1, 1,
		-1, -1,
		-1, -1,
		1, 1,
		1, -1,
	]);
	gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
	gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
}

function startRenderLoop() {
	const offCanvas = document.createElement('canvas');
	offCanvas.width = 854;
	offCanvas.height = 480;
	const ctx = offCanvas.getContext('2d');

	ctx.imageSmoothingEnabled = false;

	if ( filter.value === 'original' ) {
		canvas.width = 1920;
		canvas.height = 1080;
	} else {
		canvas.width = 2562;
		canvas.height = 1440;
	}

	let frameCount = 0;
	let lastFpsTime = 0;

	function render(now) {
		frameCount++;
		if (lastFpsTime > 0 && now - lastFpsTime >= 1000) {
			const fps = Math.round(frameCount * 1000 / (now - lastFpsTime));
			fpsDiv.textContent = `FPS: ${fps}`;
			if (!fpsReceived) {
				fpsReceived = true;
				if (uiVisible) fpsDiv.style.display = 'block';
			}
			frameCount = 0;
			lastFpsTime = now;
		} else if (lastFpsTime === 0) {
			lastFpsTime = now;
		}
		if (video.readyState >= 2) { // HAVE_CURRENT_DATA
			gl.viewport(0, 0, canvas.width, canvas.height);

			gl.bindTexture(gl.TEXTURE_2D, texture);

			const videoWidth = video.videoWidth;
			const videoHeight = video.videoHeight;

			if ( filter.value === 'original' ) {
				gl.texImage2D(
					gl.TEXTURE_2D,
					0,
					gl.RGBA,
					gl.RGBA,
					gl.UNSIGNED_BYTE,
					video
				);
			} else {
				ctx.drawImage(
					video,             // vídeo fonte
					0, 0, 1920, 1080,  // área completa do vídeo
					0, 0, 854, 480     // área do canvas a desenhar (com escala)
				);

				gl.texImage2D(
					gl.TEXTURE_2D,
					0,
					gl.RGBA,
					gl.RGBA,
					gl.UNSIGNED_BYTE,
					offCanvas
				);
			}


			gl.clearColor(0, 0, 0, 1);
			gl.clear(gl.COLOR_BUFFER_BIT);

			gl.drawArrays(gl.TRIANGLES, 0, 6);
		}

		requestAnimationFrame(render);
	}

	requestAnimationFrame(render);
}

startBtn.onclick = () => {
	startStream();
};

sidebarToggle.addEventListener('click', () => {
	sidebar.style.display = sidebar.style.display === 'none' ? 'block' : 'none';
});

filter.addEventListener('change', () => {
	['original', 'downscale', 'crt', 'crtgrainy', 'blurrycrt', 'blurrycrt2', 'blurrygrainycrt', 'sharpen', 'grainy'].forEach(c => container.classList.remove(c));
	container.classList.add(filter.value);
	if (filter.value === 'original') {
		canvas.width = 1920;
		canvas.height = 1080;
	} else {
		canvas.width = 2562;
		canvas.height = 1440;
	}
	recompileProgram();
});

aspect.addEventListener('change', () => {
	['fullhd', 'snesanalogue', 'n64retroscaler2x', 'ps1retroscaler2x', 'ps2retroscaler2x'].forEach(c => container.classList.remove(c));
	container.classList.add(aspect.value);
});

volumeSlider.addEventListener('input', () => {
	audio.volume = parseFloat(volumeSlider.value);
});

// Initialize: Load shaders first, then request permissions
loadAllShaders().then(() => {
	requestPermissionsAndListDevices();
});
