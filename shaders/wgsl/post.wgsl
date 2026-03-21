@group(0) @binding(0) var t_tex: texture_2d<f32>;
@group(0) @binding(1) var t_samp: sampler;

struct PostParams {
  brightness: f32,
  saturation: f32,
  _pad0: f32,
  _pad1: f32,
}
@group(0) @binding(2) var<uniform> params: PostParams;

struct VertexOutput {
  @builtin(position) pos: vec4f,
  @location(0) tex_coord: vec2f,
}

@vertex
fn vs_main(@location(0) position: vec2f, @location(1) tex_coord: vec2f) -> VertexOutput {
  var out: VertexOutput;
  out.pos = vec4f(position, 0.0, 1.0);
  out.tex_coord = tex_coord;
  return out;
}

@fragment
fn fs_main(in: VertexOutput) -> @location(0) vec4f {
  var c = textureSample(t_tex, t_samp, in.tex_coord);
  c = vec4f(c.rgb * params.brightness, c.a);
  let luma = dot(c.rgb, vec3f(0.2126, 0.7152, 0.0722));
  c = vec4f(mix(vec3f(luma), c.rgb, params.saturation), c.a);
  return clamp(c, vec4f(0.0), vec4f(1.0));
}
