precision highp float;

varying highp vec2 v_texCoord;
uniform sampler2D u_texture;
uniform vec2 u_textureSize;

const bool enableHorizontalScanlines = true;
const float horizontalSpacing = 1.0;     // Espaçamento em pixels da textura original
const float horizontalThickness = 0.5;   // Espessura da linha escura
const float horizontalIntensity = 0.3;   // Intensidade (0.0 a 1.0)

void main() {
  vec2 texelCoord = v_texCoord * u_textureSize;

  vec2 cellIndex = floor(texelCoord);
  vec2 localPos = fract(texelCoord) * 3.0;

  vec2 cellSize = 1.0 / u_textureSize;
  vec2 baseCoord = (cellIndex + 0.5) * cellSize;

  vec4 center = texture2D(u_texture, baseCoord);
  vec4 left   = texture2D(u_texture, (cellIndex + vec2(-1.0, 0.0) + 0.5) * cellSize);
  vec4 right  = texture2D(u_texture, (cellIndex + vec2( 1.0, 0.0) + 0.5) * cellSize);
  vec4 top    = texture2D(u_texture, (cellIndex + vec2(0.0, -1.0) + 0.5) * cellSize);
  vec4 bottom = texture2D(u_texture, (cellIndex + vec2(0.0,  1.0) + 0.5) * cellSize);

  vec4 horiz;
  if (localPos.x < 1.0) {
    horiz = mix(left, center, (localPos.x + 1.0) * 0.5);
  } else if (localPos.x > 2.0) {
    horiz = mix(center, right, (localPos.x - 2.0) * 0.5);
  } else {
    horiz = center;
  }

  vec4 finalColor;
  if (localPos.y < 1.0) {
    finalColor = mix(top, horiz, (localPos.y + 1.0) * 0.5);
  } else if (localPos.y > 2.0) {
    finalColor = mix(horiz, bottom, (localPos.y - 2.0) * 0.5);
  } else {
    finalColor = horiz;
  }

  vec2 radial = (localPos - 1.5) / 1.5;
  float dist = dot(radial, radial);
  float vignette = smoothstep(1.0, 0.0, dist);
  finalColor.rgb *= mix(0.8, 1.0, vignette);

  float chromaOffset = 0.003;
  vec4 colorR = texture2D(u_texture, baseCoord + vec2(chromaOffset, 0.0));
  vec4 colorB = texture2D(u_texture, baseCoord - vec2(chromaOffset, 0.0));
  finalColor.r = mix(finalColor.r, colorR.r, 0.15);
  finalColor.b = mix(finalColor.b, colorB.b, 0.15);

  if (enableHorizontalScanlines) {
    float y = texelCoord.y;
    float modY = mod(y, horizontalSpacing);
    if (modY < horizontalThickness) {
      finalColor.rgb *= (1.0 - horizontalIntensity);
    }
  }

  gl_FragColor = finalColor;
}
