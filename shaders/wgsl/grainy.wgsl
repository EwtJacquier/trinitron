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
  let pixel = vec2f(1.0) / params.texture_size;
  let center = textureSample(t_tex, t_samp, in.tex_coord);
  let up     = textureSample(t_tex, t_samp, in.tex_coord + vec2f(0.0,  -pixel.y));
  let down   = textureSample(t_tex, t_samp, in.tex_coord + vec2f(0.0,   pixel.y));
  let left   = textureSample(t_tex, t_samp, in.tex_coord + vec2f(-pixel.x, 0.0));
  let right  = textureSample(t_tex, t_samp, in.tex_coord + vec2f( pixel.x, 0.0));

  let rawSharpen = (5.0 * center) - up - down - left - right;
  var sharpened = mix(center, rawSharpen, 0.5);

  let minColor = min(center, min(up, min(down, min(left, right))));
  let maxColor = max(center, max(up, max(down, max(left, right))));
  sharpened = clamp(sharpened, minColor, maxColor);

  let grainAmount = 0.09;
  let noise = random(in.tex_coord * params.texture_size + vec2f(params.time * 60.0, params.time * 120.0)) - 0.5;
  sharpened = vec4f(sharpened.rgb + noise * grainAmount, sharpened.a);

  return clamp(sharpened, vec4f(0.0), vec4f(1.0));
}
