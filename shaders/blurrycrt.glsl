precision mediump float;

varying vec2 v_texCoord;
uniform sampler2D u_texture;
uniform vec2 u_textureSize;

const bool enableHorizontalScanlines = true;
const float horizontalSpacing   = 1.0;   // Espaçamento em pixels da textura original
const float horizontalThickness = 0.5;   // Espessura da linha escura
const float horizontalIntensity = 0.3;   // Intensidade (0.0 a 1.0)

// ajuste fixo do blur (0.0 = sem blur, 1.0 = blur máximo)
const float blurStrength = 1.0;

// Função para pegar amostra suavizada (5 pontos)
vec4 getBlurredSample(vec2 coord) {
  vec2 texel = 1.0 / u_textureSize;

  // amostras
  vec4 c  = texture2D(u_texture, coord);
  vec4 l  = texture2D(u_texture, coord + vec2(-texel.x, 0.0));
  vec4 r  = texture2D(u_texture, coord + vec2( texel.x, 0.0));
  vec4 t  = texture2D(u_texture, coord + vec2(0.0, -texel.y));
  vec4 b  = texture2D(u_texture, coord + vec2(0.0,  texel.y));

  // blur "puro" (sem mix ainda)
  vec4 blurred = (c * 0.4) + (l + r + t + b) * 0.15;

  // interpolação balanceada (sem deslocar a imagem)
  return mix(c, blurred, blurStrength);
}

void main() {
  vec2 texelCoord = v_texCoord * u_textureSize;

  vec2 cellIndex = floor(texelCoord);
  vec2 localPos = fract(texelCoord) * 3.0;

  vec2 cellSize = 1.0 / u_textureSize;
  vec2 baseCoord = (cellIndex + 0.5) * cellSize;

  // Todas as amostras já borradas
  vec4 center = getBlurredSample(baseCoord);
  vec4 left   = getBlurredSample((cellIndex + vec2(-1.0, 0.0) + 0.5) * cellSize);
  vec4 right  = getBlurredSample((cellIndex + vec2( 1.0, 0.0) + 0.5) * cellSize);
  vec4 top    = getBlurredSample((cellIndex + vec2(0.0, -1.0) + 0.5) * cellSize);
  vec4 bottom = getBlurredSample((cellIndex + vec2(0.0,  1.0) + 0.5) * cellSize);

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

  // Vinheta radial
  vec2 radial = (localPos - 1.5) / 1.5;
  float dist = dot(radial, radial);
  float vignette = smoothstep(1.0, 0.0, dist);
  finalColor.rgb *= mix(0.8, 1.0, vignette);

  // Aberração cromática leve (também borrada)
  float chromaOffset = 0.003;
  vec4 colorR = getBlurredSample(baseCoord + vec2(chromaOffset, 0.0));
  vec4 colorB = getBlurredSample(baseCoord - vec2(chromaOffset, 0.0));
  finalColor.r = mix(finalColor.r, colorR.r, 0.15);
  finalColor.b = mix(finalColor.b, colorB.b, 0.15);

  // Scanlines horizontais
  if (enableHorizontalScanlines) {
    float y = texelCoord.y;
    float modY = mod(y, horizontalSpacing);
    if (modY < horizontalThickness) {
      finalColor.rgb *= (1.0 - horizontalIntensity);
    }
  }

  gl_FragColor = finalColor;
}
