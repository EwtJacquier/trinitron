@group(0) @binding(0) var t_tex: texture_2d<f32>;
@group(0) @binding(1) var t_samp: sampler;

struct Params {
  texture_size: vec2f,
  time: f32,
  _pad: f32,
}
@group(0) @binding(2) var<uniform> params: Params;

fn random(co: vec2f) -> f32 {
  return fract(sin(dot(co, vec2f(12.9898, 78.233))) * 43758.5453);
}

@fragment
fn fs_main(in: VertexOutput) -> @location(0) vec4f {
  let texelCoord = in.tex_coord * params.texture_size;
  let cellIndex = floor(texelCoord);
  let localPos = fract(texelCoord) * 3.0;
  let cellSize = vec2f(1.0) / params.texture_size;
  let baseCoord = (cellIndex + vec2f(0.5)) * cellSize;

  let center = textureSample(t_tex, t_samp, baseCoord);
  let left   = textureSample(t_tex, t_samp, (cellIndex + vec2f(-1.0, 0.0) + vec2f(0.5)) * cellSize);
  let right  = textureSample(t_tex, t_samp, (cellIndex + vec2f( 1.0, 0.0) + vec2f(0.5)) * cellSize);
  let top    = textureSample(t_tex, t_samp, (cellIndex + vec2f(0.0, -1.0) + vec2f(0.5)) * cellSize);
  let bottom = textureSample(t_tex, t_samp, (cellIndex + vec2f(0.0,  1.0) + vec2f(0.5)) * cellSize);

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
  let colorR = textureSample(t_tex, t_samp, baseCoord + vec2f(chromaOffset, 0.0));
  let colorB = textureSample(t_tex, t_samp, baseCoord - vec2f(chromaOffset, 0.0));
  finalColor.r = mix(finalColor.r, colorR.r, 0.15);
  finalColor.b = mix(finalColor.b, colorB.b, 0.15);

  // Horizontal scanlines (spacing=1.0, thickness=0.5, intensity=0.3)
  let modY = texelCoord.y % 1.0;
  if (modY < 0.5) {
    finalColor = vec4f(finalColor.rgb * (1.0 - 0.3), finalColor.a);
  }

  let grainAmount = 0.09;
  let noise = random(in.tex_coord * params.texture_size + vec2f(params.time * 60.0, params.time * 120.0)) - 0.5;
  finalColor = vec4f(finalColor.rgb + noise * grainAmount, finalColor.a);

  return clamp(finalColor, vec4f(0.0), vec4f(1.0));
}
