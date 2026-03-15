const cameraSelect = document.getElementById('cameraSelect');
const micSelect = document.getElementById('micSelect');
const aspect = document.getElementById('ratio');
const filter = document.getElementById('filter');
const startBtn = document.getElementById('startBtn');
const container = document.getElementById('container');
const menu = document.getElementById('menu');
const sidebar = document.getElementById('sidebar');
const sidebarToggle = document.getElementById('sidebarToggle');
const volumeSlider = document.getElementById('volume');
const video = document.getElementById('video');
const audio = document.getElementById('audio');
const canvas = document.getElementById('canvas');
const fpsDiv = document.getElementById('fps');

let videoStream;
let audioStream;
let sourceFps = 0;

// WebGPU state
let gpuDevice, gpuContext, gpuFormat;
let pipeline;
let vertexBuffer, texCoordBuffer;
let gpuTexture, sampler;
let uniformBuffer;
let bindGroup;

// Track current texture dimensions to detect changes
let currentTexWidth = 0;
let currentTexHeight = 0;

// Shader sources
const shaders = {};
let vsSource = null;

// Load shader from file
async function loadShader(name, isWgsl) {
	const path = isWgsl ? `shaders/wgsl/${name}.wgsl` : `shaders/${name}.glsl`;
	const response = await fetch(path);
	return await response.text();
}

async function loadAllShaders() {
	const shaderNames = [
		'original',
		'downscale',
		'crt',
		'crtgrainy',
		'blurrycrt',
		'blurrygrainycrt',
		'sharpen',
		'grainy'
	];

	vsSource = await loadShader('vertex', true);
	for (const name of shaderNames) {
		shaders[name] = await loadShader(name, true);
	}
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

		// Read real source FPS from the video track
		const videoTrack = videoStream.getVideoTracks()[0];
		sourceFps = videoTrack.getSettings().frameRate || 0;

		video.onloadedmetadata = async () => {
			video.play();
			menu.style.display = 'none';
			sidebar.style.display = 'block';
			sidebarToggle.style.display = 'block';
			fpsDiv.style.display = 'block';
			await initWebGPU();
			startRenderLoop();
		};
	} catch (err) {
		alert("Error starting media: " + err.message);
		console.error(err);
	}
}

async function initWebGPU() {
	if (!navigator.gpu) {
		alert('WebGPU not supported in this browser.');
		return;
	}

	const adapter = await navigator.gpu.requestAdapter();
	if (!adapter) {
		alert('No WebGPU adapter found.');
		return;
	}

	gpuDevice = await adapter.requestDevice();

	gpuContext = canvas.getContext('webgpu');
	gpuFormat = navigator.gpu.getPreferredCanvasFormat();

	gpuContext.configure({
		device: gpuDevice,
		format: gpuFormat,
		alphaMode: 'opaque',
	});

	// Full-screen quad: 6 vertices, each with position (vec2f) and texcoord (vec2f)
	// Position buffer: NDC coordinates
	const positions = new Float32Array([
		-1,  1,
		 1,  1,
		-1, -1,
		-1, -1,
		 1,  1,
		 1, -1,
	]);
	vertexBuffer = gpuDevice.createBuffer({
		size: positions.byteLength,
		usage: GPUBufferUsage.VERTEX | GPUBufferUsage.COPY_DST,
	});
	gpuDevice.queue.writeBuffer(vertexBuffer, 0, positions);

	// TexCoord buffer
	const texCoords = new Float32Array([
		0, 0,
		1, 0,
		0, 1,
		0, 1,
		1, 0,
		1, 1,
	]);
	texCoordBuffer = gpuDevice.createBuffer({
		size: texCoords.byteLength,
		usage: GPUBufferUsage.VERTEX | GPUBufferUsage.COPY_DST,
	});
	gpuDevice.queue.writeBuffer(texCoordBuffer, 0, texCoords);

	// Uniform buffer: vec2f (textureSize) + f32 (time) + f32 (pad) = 16 bytes
	uniformBuffer = gpuDevice.createBuffer({
		size: 16,
		usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
	});

	// Sampler
	sampler = gpuDevice.createSampler({
		addressModeU: 'clamp-to-edge',
		addressModeV: 'clamp-to-edge',
		minFilter: 'linear',
		magFilter: 'linear',
	});

	await recompilePipeline();

	window.addEventListener('resize', () => {
		// No-op: canvas size is fixed, CSS scales it
	});
}

async function recompilePipeline() {
	if (!gpuDevice) return;

	const fsSource = shaders[filter.value];
	if (!fsSource) {
		console.error('No shader found for filter:', filter.value);
		return;
	}

	const shaderCode = vsSource + '\n' + fsSource;
	const shaderModule = gpuDevice.createShaderModule({ code: shaderCode });

	pipeline = gpuDevice.createRenderPipeline({
		layout: 'auto',
		vertex: {
			module: shaderModule,
			entryPoint: 'vs_main',
			buffers: [
				{
					arrayStride: 8, // 2x f32
					attributes: [{ shaderLocation: 0, offset: 0, format: 'float32x2' }],
				},
				{
					arrayStride: 8, // 2x f32
					attributes: [{ shaderLocation: 1, offset: 0, format: 'float32x2' }],
				},
			],
		},
		fragment: {
			module: shaderModule,
			entryPoint: 'fs_main',
			targets: [{ format: gpuFormat }],
		},
		primitive: { topology: 'triangle-list' },
	});

	// Set initial uniform values
	const isOriginal = filter.value === 'original';
	const tw = isOriginal ? 2562.0 : 640.0;
	const th = isOriginal ? 1440.0 : 360.0;
	gpuDevice.queue.writeBuffer(uniformBuffer, 0, new Float32Array([tw, th, 0.0, 0.0]));

	// Create placeholder texture (1x1) if none exists yet
	if (!gpuTexture || currentTexWidth === 0) {
		createGpuTexture(1, 1);
	}

	// Recreate bind group with new pipeline layout
	rebuildBindGroup();
}

function createGpuTexture(width, height) {
	if (gpuTexture) gpuTexture.destroy();
	gpuTexture = gpuDevice.createTexture({
		size: [width, height, 1],
		format: 'rgba8unorm',
		usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST | GPUTextureUsage.RENDER_ATTACHMENT,
	});
	currentTexWidth = width;
	currentTexHeight = height;
}

function rebuildBindGroup() {
	if (!pipeline || !gpuTexture) return;
	bindGroup = gpuDevice.createBindGroup({
		layout: pipeline.getBindGroupLayout(0),
		entries: [
			{ binding: 0, resource: gpuTexture.createView() },
			{ binding: 1, resource: sampler },
			{ binding: 2, resource: { buffer: uniformBuffer } },
		],
	});
}

function startRenderLoop() {
	const offCanvas = document.createElement('canvas');
	offCanvas.width = 854;
	offCanvas.height = 480;
	const ctx = offCanvas.getContext('2d');
	ctx.imageSmoothingEnabled = false;

	const startTime = performance.now();
	let frameCount = 0;
	let lastFpsUpdate = startTime;
	let renderFps = 0;

	function render(now) {
		if (video.readyState >= 2) {
			const isOriginal = filter.value === 'original';

			let sourceWidth, sourceHeight, sourceElement;
			if (isOriginal) {
				sourceWidth = video.videoWidth;
				sourceHeight = video.videoHeight;
				sourceElement = video;
			} else {
				ctx.drawImage(video, 0, 0, 1920, 1080, 0, 0, 854, 480);
				sourceWidth = 854;
				sourceHeight = 480;
				sourceElement = offCanvas;
			}

			// Recreate GPU texture if dimensions changed
			if (sourceWidth !== currentTexWidth || sourceHeight !== currentTexHeight) {
				createGpuTexture(sourceWidth, sourceHeight);
				rebuildBindGroup();
			}

			// Upload video frame to GPU texture
			gpuDevice.queue.copyExternalImageToTexture(
				{ source: sourceElement, flipY: false },
				{ texture: gpuTexture },
				[sourceWidth, sourceHeight]
			);

			// Update uniform buffer (textureSize + time)
			const tw = isOriginal ? 2562.0 : 640.0;
			const th = isOriginal ? 1440.0 : 360.0;
			const time = (now - startTime) / 1000.0;
			gpuDevice.queue.writeBuffer(uniformBuffer, 0, new Float32Array([tw, th, time, 0.0]));

			// Encode and submit render pass
			const encoder = gpuDevice.createCommandEncoder();
			const pass = encoder.beginRenderPass({
				colorAttachments: [{
					view: gpuContext.getCurrentTexture().createView(),
					loadOp: 'clear',
					storeOp: 'store',
					clearValue: { r: 0, g: 0, b: 0, a: 1 },
				}],
			});
			pass.setPipeline(pipeline);
			pass.setBindGroup(0, bindGroup);
			pass.setVertexBuffer(0, vertexBuffer);
			pass.setVertexBuffer(1, texCoordBuffer);
			pass.draw(6);
			pass.end();
			gpuDevice.queue.submit([encoder.finish()]);

			// FPS counter
			frameCount++;
			if (now - lastFpsUpdate >= 1000) {
				renderFps = Math.round(frameCount * 1000 / (now - lastFpsUpdate));
				frameCount = 0;
				lastFpsUpdate = now;
				const srcLabel = sourceFps ? Math.round(sourceFps) + 'fps' : '?fps';
				fpsDiv.textContent = `Source: ${srcLabel} | Render: ${renderFps}fps`;
			}
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

filter.addEventListener('change', async () => {
	['original', 'downscale', 'crt', 'crtgrainy', 'blurrycrt', 'blurrycrt2', 'blurrygrainycrt', 'sharpen', 'grainy'].forEach(c => container.classList.remove(c));
	container.classList.add(filter.value);
	// Reset texture dimensions so it gets recreated on next frame
	currentTexWidth = 0;
	currentTexHeight = 0;
	await recompilePipeline();
});

aspect.addEventListener('change', () => {
	['fullhd', 'snesanalogue', 'n64retroscaler2x', 'ps1retroscaler2x', 'ps2retroscaler2x'].forEach(c => container.classList.remove(c));
	container.classList.add(aspect.value);
});

volumeSlider.addEventListener('input', () => {
	audio.volume = parseFloat(volumeSlider.value);
});

// Initialize: load shaders, then request permissions
loadAllShaders().then(() => {
	requestPermissionsAndListDevices();
});
