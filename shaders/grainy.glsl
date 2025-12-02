precision mediump float;

varying vec2 v_texCoord;
uniform sampler2D u_texture;
uniform vec2 u_textureSize;
uniform float u_time; // tempo em segundos, passado pelo JS para animar o grain

// Função para gerar ruído pseudo-aleatório mais forte
float random(vec2 co) {
    return fract(sin(dot(co, vec2(12.9898,78.233))) * 43758.5453);
}

void main() {
    vec2 pixel = 1.0 / u_textureSize;

    vec4 center = texture2D(u_texture, v_texCoord);
    vec4 up     = texture2D(u_texture, v_texCoord + vec2(0.0, -pixel.y));
    vec4 down   = texture2D(u_texture, v_texCoord + vec2(0.0,  pixel.y));
    vec4 left   = texture2D(u_texture, v_texCoord + vec2(-pixel.x, 0.0));
    vec4 right  = texture2D(u_texture, v_texCoord + vec2( pixel.x, 0.0));

    vec4 rawSharpen = (5.0 * center) - up - down - left - right;
    vec4 sharpened = mix(center, rawSharpen, 0.5);
    sharpened = clamp(sharpened, 0.0, 1.0);

    float contrast = length(center.rgb - 0.25 * (up.rgb + down.rgb + left.rgb + right.rgb));

    vec4 smoothColor = 0.25 * (left + right + up + down);

    float edgeThreshold = 0.1;
    float blendFactor = smoothstep(edgeThreshold, 0.5, contrast);
    vec4 finalColor = mix(sharpened, smoothColor, blendFactor);

    float grainAmount = 0.09; // intensidade do ruído
    float noise = random(v_texCoord * u_textureSize + vec2(u_time * 60.0, u_time * 120.0)) - 0.5;
    finalColor.rgb += noise * grainAmount;

    gl_FragColor = clamp(finalColor, 0.0, 1.0);
}
