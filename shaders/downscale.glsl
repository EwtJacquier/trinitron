precision mediump float;

varying vec2 v_texCoord;
uniform sampler2D u_texture;
uniform vec2 u_textureSize;

void main() {
  vec2 cellSize = 1.0 / u_textureSize;

  vec2 pos = v_texCoord * (u_textureSize * 3.0);

  vec2 cellIndex = floor(pos / 3.0);

  vec2 baseCoord = (cellIndex + 0.5) * cellSize;

  vec4 color = texture2D(u_texture, baseCoord);

  gl_FragColor = color;
}
