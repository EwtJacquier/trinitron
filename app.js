const cameraSelect = document.getElementById('cameraSelect');
const micSelect = document.getElementById('micSelect');
const aspect = document.getElementById('ratio');
const filter = document.getElementById('filter');
const startBtn = document.getElementById('startBtn');
const container = document.getElementById('container');
const menu = document.getElementById('menu');
const video = document.getElementById('video');
const audio = document.getElementById('audio');
const canvas = document.getElementById('canvas');
const fpsDisplay = document.getElementById('fps');

let videoStream;
let audioStream;
let gl;
let program;
let positionBuffer;
let texCoordBuffer;
let texture;
let lastTime = performance.now();
let frames = 0;

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

	program = createProgram(gl, vsSource, null);
	gl.useProgram(program);

	const positionLocation = gl.getAttribLocation(program, "a_position");
	const texCoordLocation = gl.getAttribLocation(program, "a_texCoord");

	positionBuffer = gl.createBuffer();
	gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
	setPositionFullScreen();

	gl.enableVertexAttribArray(positionLocation);
	gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 0, 0);

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

	gl.enableVertexAttribArray(texCoordLocation);
	gl.vertexAttribPointer(texCoordLocation, 2, gl.FLOAT, false, 0, 0);

	texture = gl.createTexture();
	gl.bindTexture(gl.TEXTURE_2D, texture);

	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

	videoAspect = video.videoWidth / video.videoHeight;

	gl.uniform2f(gl.getUniformLocation(program, "u_textureSize"), filter.value === 'original' ? 1920.0 : 640.0, filter.value === 'original' ? 1080.0 : 360.0);

	window.addEventListener('resize', () => {
		updatePositionsForAspectRatio();
	});

	updatePositionsForAspectRatio();
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
	}

	function render(now) {
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

			frames++;
			const nowMs = performance.now();
			if (nowMs - lastTime >= 1000) {
				fpsDisplay.textContent = `FPS: ${frames}`;
				frames = 0;
				lastTime = nowMs;
			}
		}

		requestAnimationFrame(render);
	}

	requestAnimationFrame(render);
}

startBtn.onclick = () => {
	startStream();
};

// Initialize: Load shaders first, then request permissions
loadAllShaders().then(() => {
	requestPermissionsAndListDevices();
});
