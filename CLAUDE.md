# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Trinitron is a WebGL-based CRT (Cathode Ray Tube) webcam viewer that applies real-time retro visual filters to webcam feeds. The entire application is a single-file HTML application with embedded JavaScript and GLSL shaders.

## Architecture

### Single-File Structure
The entire application is contained in `index.html` with three main sections:
1. **CSS Styles** (lines 7-119): Defines UI layout, canvas positioning, and filter-specific CSS classes
2. **HTML Structure** (lines 123-159): Menu UI, video/audio elements, canvas, and FPS counter
3. **JavaScript Application** (lines 161-888): WebGL initialization, shader management, media stream handling

### WebGL Shader Pipeline
The application uses a fragment shader-based approach where all visual effects are implemented as GLSL shaders:

- **Vertex Shader** (lines 619-629): Simple passthrough that maps texture coordinates
- **Fragment Shaders** (lines 186-612): Eight different filters stored in the `filters` object:
  - `original`: Direct passthrough (no processing)
  - `downscale`: 480p downscaling with cell-based sampling
  - `crt`: CRT blend with scanlines, vignette, and chromatic aberration
  - `crtgrainy`: CRT blend + animated grain noise
  - `blurrycrt`: CRT blend + 5-point blur sampling
  - `blurrygrainycrt`: CRT blend + blur + grain
  - `sharpen`: Edge-preserving sharpening for 3D content
  - `grainy`: Sharpen + animated grain

### Rendering Flow
1. Webcam video is captured at 1920x1080@60fps (ideal)
2. For non-original filters, video is downscaled to 854x480 on an off-screen canvas (lines 815-819)
3. The downscaled/original texture is uploaded to WebGL (lines 836-860)
4. Fragment shader processes each pixel to upscale to canvas resolution (2562x1440)
5. CSS filters add final brightness/saturation adjustments (lines 28-34)

### Aspect Ratio System
The `#container` element adapts to different console aspect ratios via CSS classes (lines 76-97):
- `fullhd`: 1920/1080 (modern 16:9)
- `snesanalogue`: 260/240 (Super Nintendo)
- `n64retroscaler2x`, `ps1retroscaler2x`, `ps2retroscaler2x`: 280/240 (retro consoles)

### Media Device Handling
- Permission request and device enumeration: lines 659-685
- Separate video and audio streams with device selection
- Video constraints target 60fps at 1920x1080 (lines 695-700)
- Audio has all processing disabled for minimal latency (lines 702-708)

## Key Technical Details

### Shader Compilation
- Shaders are compiled dynamically based on selected filter (line 644)
- Filter selection from `filters` object determines which fragment shader is used
- The `u_textureSize` uniform differs between original (1920x1080) and processed (640x360) modes (line 776)

### CRT Effects Implementation
CRT-style filters use several techniques:
- **Pixel blending**: Samples neighboring pixels and interpolates (lines 233-258 in `crt` shader)
- **Scanlines**: Horizontal dark lines via modulo operation (lines 272-278)
- **Vignette**: Radial darkening from edges (lines 261-264)
- **Chromatic aberration**: Red/blue channel offset (lines 266-270)
- **Blur**: 5-point sampling with configurable strength (lines 372-387 in `blurrycrt`)
- **Grain**: Time-animated pseudo-random noise (lines 351-353 in `crtgrainy`)

### Performance Characteristics
- Unlimited frame rate (no FPS cap per title)
- FPS counter updates every second (lines 868-874)
- WebGL viewport matches canvas dimensions (line 829)
- Image smoothing disabled for downscaling canvas (line 820)

## Development Workflow

Since this is a single HTML file with no build system:

1. **Testing changes**: Open `index.html` directly in a web browser (requires HTTPS or localhost for webcam access)
2. **Shader development**: Modify GLSL code in the `filters` object (lines 186-612)
3. **UI changes**: Update HTML structure (lines 123-159) or CSS (lines 7-119)
4. **Logic changes**: Modify JavaScript (lines 161-888)

### Adding New Filters
1. Add new GLSL fragment shader string to `filters` object
2. Add corresponding option to `#filter` select element (lines 140-149)
3. Optionally add CSS class for post-processing in styles section (lines 28-40)
4. Consider whether filter needs `u_time` uniform for animation (see `crtgrainy`, `blurrygrainycrt`, `grainy`)

### Commit Message Style
Recent commits follow the pattern: `[Action] Description`
- `[Fix]`: Bug fixes or corrections
- `[Add]`: New features or filters

Examples from git history:
- `[Fix] Blur Intensity`
- `[Add] Two New Filters`
- `[Add] Grainy CRT`

## Important Constants

- **Canvas resolution**: 2562x1440 (line 157)
- **Downscale resolution**: 854x480 (line 816)
- **Texture size for filters**: 640x360 (line 776, non-original modes)
- **Target framerate**: 60fps (line 697)
- **Scanline spacing**: 1.0 pixel (line 224)
- **Grain intensity**: 0.09 (lines 351, 538, 607)
- **Blur strength**: 1.0 (max, lines 369, 460)
