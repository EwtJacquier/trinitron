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
  let cellSize = vec2f(1.0) / params.texture_size;
  let pos = in.tex_coord * (params.texture_size * 3.0);
  let cellIndex = floor(pos / 3.0);
  let baseCoord = (cellIndex + vec2f(0.5)) * cellSize;
  return textureSample(t_tex, t_samp, baseCoord);
}
