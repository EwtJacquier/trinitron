precision mediump float;

varying vec2 v_texCoord;
uniform sampler2D u_texture;
uniform vec2 u_textureSize;

void main() {
  vec2 pixel = 1.0 / u_textureSize;

  vec4 center = texture2D(u_texture, v_texCoord);
  vec4 up     = texture2D(u_texture, v_texCoord + vec2(0.0, -pixel.y));
  vec4 down   = texture2D(u_texture, v_texCoord + vec2(0.0,  pixel.y));
  vec4 left   = texture2D(u_texture, v_texCoord + vec2(-pixel.x, 0.0));
  vec4 right  = texture2D(u_texture, v_texCoord + vec2( pixel.x, 0.0));

  vec4 rawSharpen = (5.0 * center) - up - down - left - right;
  vec4 sharpened = mix(center, rawSharpen, 0.5); // ajuste fino da nitidez
  sharpened = clamp(sharpened, 0.0, 1.0);

  float contrast = length(center.rgb - 0.25 * (up.rgb + down.rgb + left.rgb + right.rgb));

  vec4 smoothColor = 0.25 * (left + right + up + down);

  float edgeThreshold = 0.1;
  float blendFactor = smoothstep(edgeThreshold, 0.5, contrast);

  vec4 finalColor = mix(sharpened, smoothColor, blendFactor);

  gl_FragColor = finalColor;
}
