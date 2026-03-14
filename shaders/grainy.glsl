precision mediump float;

varying vec2 v_texCoord;
uniform sampler2D u_texture;
uniform vec2 u_textureSize;
uniform float u_time;

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

    // Limita ao intervalo local para evitar halos e cores novas nas bordas
    vec4 minColor = min(center, min(up, min(down, min(left, right))));
    vec4 maxColor = max(center, max(up, max(down, max(left, right))));
    sharpened = clamp(sharpened, minColor, maxColor);

    float grainAmount = 0.09;
    float noise = random(v_texCoord * u_textureSize + vec2(u_time * 60.0, u_time * 120.0)) - 0.5;
    sharpened.rgb += noise * grainAmount;

    gl_FragColor = clamp(sharpened, 0.0, 1.0);
}
