precision highp float;

// Revive + FXAA: FXAA 3.11 (preset "quality"), single-pass, sobre o video em 1080p.
// So age onde ha aresta (contraste local acima do limiar), descobre a orientacao da aresta e procura
// os dois extremos dela; entao desloca a amostra na perpendicular, proporcional a posicao do pixel
// dentro do segmento. E um blend ao longo da aresta existente - nao cria borda nem escurece nada.
// Area plana, textura de baixo contraste e o miolo de texto ficam abaixo do limiar e nao mudam.

varying highp vec2 v_texCoord;
uniform sampler2D u_texture;
uniform vec2 u_textureSize;

const vec3 LUMA = vec3(0.299, 0.587, 0.114);

// Contraste minimo (absoluto e relativo ao maximo local) pra considerar aresta
const float EDGE_THRESHOLD_MIN = 0.0312;
const float EDGE_THRESHOLD_MAX = 0.125;
// Passos da busca pelos extremos da aresta
const int   ITERATIONS = 12;
// Suavizacao sub-pixel (0 = nenhuma, 1 = maxima). 0.75 e o padrao do FXAA; baixar se texto amaciar demais
const float SUBPIXEL_QUALITY = 0.75;

float lumaAt(vec2 uv) {
  return dot(texture2D(u_texture, uv).rgb, LUMA);
}

float quality(int i) {
  if (i < 5) return 1.0;
  if (i == 5) return 1.5;
  if (i < 10) return 2.0;
  if (i == 10) return 4.0;
  return 8.0;
}

void main() {
  vec2 texel = 1.0 / u_textureSize;
  vec2 uv = v_texCoord;
  vec4 colorCenter = texture2D(u_texture, uv);

  float lumaCenter = dot(colorCenter.rgb, LUMA);
  float lumaDown  = lumaAt(uv + vec2(0.0, -texel.y));
  float lumaUp    = lumaAt(uv + vec2(0.0,  texel.y));
  float lumaLeft  = lumaAt(uv + vec2(-texel.x, 0.0));
  float lumaRight = lumaAt(uv + vec2( texel.x, 0.0));

  float lumaMin = min(lumaCenter, min(min(lumaDown, lumaUp), min(lumaLeft, lumaRight)));
  float lumaMax = max(lumaCenter, max(max(lumaDown, lumaUp), max(lumaLeft, lumaRight)));
  float lumaRange = lumaMax - lumaMin;

  // Sem aresta: passthrough
  if (lumaRange < max(EDGE_THRESHOLD_MIN, lumaMax * EDGE_THRESHOLD_MAX)) {
    gl_FragColor = colorCenter;
    return;
  }

  float lumaDownLeft  = lumaAt(uv + vec2(-texel.x, -texel.y));
  float lumaUpRight   = lumaAt(uv + vec2( texel.x,  texel.y));
  float lumaUpLeft    = lumaAt(uv + vec2(-texel.x,  texel.y));
  float lumaDownRight = lumaAt(uv + vec2( texel.x, -texel.y));

  float lumaDownUp    = lumaDown + lumaUp;
  float lumaLeftRight = lumaLeft + lumaRight;
  float lumaLeftCorners  = lumaDownLeft + lumaUpLeft;
  float lumaDownCorners  = lumaDownLeft + lumaDownRight;
  float lumaRightCorners = lumaDownRight + lumaUpRight;
  float lumaUpCorners    = lumaUpRight + lumaUpLeft;

  // Orientacao da aresta
  float edgeHorizontal = abs(-2.0 * lumaLeft + lumaLeftCorners) + abs(-2.0 * lumaCenter + lumaDownUp) * 2.0 + abs(-2.0 * lumaRight + lumaRightCorners);
  float edgeVertical   = abs(-2.0 * lumaUp + lumaUpCorners)     + abs(-2.0 * lumaCenter + lumaLeftRight) * 2.0 + abs(-2.0 * lumaDown + lumaDownCorners);
  bool isHorizontal = edgeHorizontal >= edgeVertical;

  // Lado da aresta com o gradiente mais forte
  float luma1 = isHorizontal ? lumaDown : lumaLeft;
  float luma2 = isHorizontal ? lumaUp : lumaRight;
  float gradient1 = luma1 - lumaCenter;
  float gradient2 = luma2 - lumaCenter;
  bool is1Steepest = abs(gradient1) >= abs(gradient2);
  float gradientScaled = 0.25 * max(abs(gradient1), abs(gradient2));

  float stepLength = isHorizontal ? texel.y : texel.x;
  float lumaLocalAverage = 0.0;
  if (is1Steepest) {
    stepLength = -stepLength;
    lumaLocalAverage = 0.5 * (luma1 + lumaCenter);
  } else {
    lumaLocalAverage = 0.5 * (luma2 + lumaCenter);
  }

  // Meio caminho pra aresta, depois busca os extremos ao longo dela
  vec2 currentUv = uv;
  if (isHorizontal) currentUv.y += stepLength * 0.5;
  else currentUv.x += stepLength * 0.5;

  vec2 offset = isHorizontal ? vec2(texel.x, 0.0) : vec2(0.0, texel.y);
  vec2 uv1 = currentUv - offset;
  vec2 uv2 = currentUv + offset;

  float lumaEnd1 = lumaAt(uv1) - lumaLocalAverage;
  float lumaEnd2 = lumaAt(uv2) - lumaLocalAverage;
  bool reached1 = abs(lumaEnd1) >= gradientScaled;
  bool reached2 = abs(lumaEnd2) >= gradientScaled;
  bool reachedBoth = reached1 && reached2;

  if (!reached1) uv1 -= offset;
  if (!reached2) uv2 += offset;

  if (!reachedBoth) {
    for (int i = 2; i < ITERATIONS; i++) {
      if (!reached1) { lumaEnd1 = lumaAt(uv1) - lumaLocalAverage; }
      if (!reached2) { lumaEnd2 = lumaAt(uv2) - lumaLocalAverage; }
      reached1 = abs(lumaEnd1) >= gradientScaled;
      reached2 = abs(lumaEnd2) >= gradientScaled;
      reachedBoth = reached1 && reached2;
      if (!reached1) uv1 -= offset * quality(i);
      if (!reached2) uv2 += offset * quality(i);
      if (reachedBoth) break;
    }
  }

  float distance1 = isHorizontal ? (uv.x - uv1.x) : (uv.y - uv1.y);
  float distance2 = isHorizontal ? (uv2.x - uv.x) : (uv2.y - uv.y);
  bool isDirection1 = distance1 < distance2;
  float distanceFinal = min(distance1, distance2);
  float edgeThickness = distance1 + distance2;
  float pixelOffset = -distanceFinal / edgeThickness + 0.5;

  // So desloca se a variacao de luma no extremo mais proximo e coerente com o centro
  bool isLumaCenterSmaller = lumaCenter < lumaLocalAverage;
  bool correctVariation = ((isDirection1 ? lumaEnd1 : lumaEnd2) < 0.0) != isLumaCenterSmaller;
  float finalOffset = correctVariation ? pixelOffset : 0.0;

  // Sub-pixel: detalhe menor que um pixel (linha fina) recebe um blend leve com a media 3x3
  float lumaAverage = (1.0 / 12.0) * (2.0 * (lumaDownUp + lumaLeftRight) + lumaLeftCorners + lumaRightCorners);
  float subPixelOffset1 = clamp(abs(lumaAverage - lumaCenter) / lumaRange, 0.0, 1.0);
  float subPixelOffset2 = (-2.0 * subPixelOffset1 + 3.0) * subPixelOffset1 * subPixelOffset1;
  float subPixelOffsetFinal = subPixelOffset2 * subPixelOffset2 * SUBPIXEL_QUALITY;
  finalOffset = max(finalOffset, subPixelOffsetFinal);

  vec2 finalUv = uv;
  if (isHorizontal) finalUv.y += finalOffset * stepLength;
  else finalUv.x += finalOffset * stepLength;

  gl_FragColor = vec4(texture2D(u_texture, finalUv).rgb, colorCenter.a);
}
