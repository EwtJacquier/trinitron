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
