@group(0) @binding(0) var t_tex: texture_2d<f32>;
@group(0) @binding(1) var t_samp: sampler;

struct Params {
  texture_size: vec2f,
  time: f32,
  _pad: f32,
}
@group(0) @binding(2) var<uniform> params: Params;

fn getBlurredSample(coord: vec2f) -> vec4f {
  let texel = vec2f(1.0) / params.texture_size;
  let c = textureSample(t_tex, t_samp, coord);
  let l = textureSample(t_tex, t_samp, coord + vec2f(-texel.x, 0.0));
  let r = textureSample(t_tex, t_samp, coord + vec2f( texel.x, 0.0));
  let t = textureSample(t_tex, t_samp, coord + vec2f(0.0, -texel.y));
  let b = textureSample(t_tex, t_samp, coord + vec2f(0.0,  texel.y));
  let blurred = (c * 0.4) + (l + r + t + b) * 0.15;
  return mix(c, blurred, 1.0); // blurStrength = 1.0
}

@fragment
fn fs_main(in: VertexOutput) -> @location(0) vec4f {
  let texelCoord = in.tex_coord * params.texture_size;
  let cellIndex = floor(texelCoord);
  let localPos = fract(texelCoord) * 3.0;
  let cellSize = vec2f(1.0) / params.texture_size;
  let baseCoord = (cellIndex + vec2f(0.5)) * cellSize;

  let center = getBlurredSample(baseCoord);
  let left   = getBlurredSample((cellIndex + vec2f(-1.0, 0.0) + vec2f(0.5)) * cellSize);
  let right  = getBlurredSample((cellIndex + vec2f( 1.0, 0.0) + vec2f(0.5)) * cellSize);
  let top    = getBlurredSample((cellIndex + vec2f(0.0, -1.0) + vec2f(0.5)) * cellSize);
  let bottom = getBlurredSample((cellIndex + vec2f(0.0,  1.0) + vec2f(0.5)) * cellSize);

  var horiz: vec4f;
  if (localPos.x < 1.0) {
    horiz = mix(left, center, (localPos.x + 1.0) * 0.5);
  } else if (localPos.x > 2.0) {
    horiz = mix(center, right, (localPos.x - 2.0) * 0.5);
  } else {
    horiz = center;
  }

  var finalColor: vec4f;
  if (localPos.y < 1.0) {
    finalColor = mix(top, horiz, (localPos.y + 1.0) * 0.5);
  } else if (localPos.y > 2.0) {
    finalColor = mix(horiz, bottom, (localPos.y - 2.0) * 0.5);
  } else {
    finalColor = horiz;
  }

  let radial = (localPos - vec2f(1.5)) / 1.5;
  let dist = dot(radial, radial);
  let vignette = smoothstep(1.0, 0.0, dist);
  finalColor = vec4f(finalColor.rgb * mix(0.8, 1.0, vignette), finalColor.a);

  let chromaOffset = 0.003;
  let colorR = getBlurredSample(baseCoord + vec2f(chromaOffset, 0.0));
  let colorB = getBlurredSample(baseCoord - vec2f(chromaOffset, 0.0));
  finalColor.r = mix(finalColor.r, colorR.r, 0.15);
  finalColor.b = mix(finalColor.b, colorB.b, 0.15);

  // Horizontal scanlines (spacing=1.0, thickness=0.5, intensity=0.3)
  let modY = texelCoord.y % 1.0;
  if (modY < 0.5) {
    finalColor = vec4f(finalColor.rgb * (1.0 - 0.3), finalColor.a);
  }

  return finalColor;
}
