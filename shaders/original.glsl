precision mediump float;

varying vec2 v_texCoord;
uniform sampler2D u_texture;
uniform vec2 u_textureSize;

void main() {
  vec4 color = texture2D(u_texture, v_texCoord);
  gl_FragColor = color;
}
