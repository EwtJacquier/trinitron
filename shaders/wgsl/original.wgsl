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
  return textureSample(t_tex, t_samp, in.tex_coord);
}
