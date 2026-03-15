@group(0) @binding(0) var t_tex: texture_2d<f32>;
@group(0) @binding(1) var t_samp: sampler;

struct Params {
  texture_size: vec2f,
  time: f32,
  _pad: f32,
}
@group(0) @binding(2) var<uniform> params: Params;

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

  return sharpened;
}
