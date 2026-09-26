#version 440
// Liquid Glass: a slab of glass whose rim is a squircle-profiled bevel.
//
// Per pixel, the shape's signed distance field gives the distance to the rim and the
// outward normal. The rim's height profile h(x) = (1 - (1-x)^4)^(1/4) (x = 0 at the rim,
// 1 where the top goes flat) gives the surface tilt; Snell's law (air -> glass, n = 1.5)
// turns that tilt into how far the ray through this pixel is bent toward the centre, so
// the background is sampled from further inside: edges magnify and bend, the flat middle
// stays clear. Red, green and blue bend by slightly different amounts (dispersion), a
// thin specular rim catches a directional light (bright on the lit edge, fainter on the
// opposite one), a Fresnel sheen lights the steep part of the bevel, and an inner shadow
// on the far side gives depth. Outside the shape a soft drop shadow is drawn from the
// same distance field. Everything is analytic: no displacement-map image.

layout(location = 0) in vec2 vPix;
layout(location = 1) flat in vec4 vRect;
layout(location = 2) flat in vec4 vShape;
layout(location = 3) flat in vec4 vOptics;
layout(location = 4) flat in vec4 vTint;
layout(location = 5) flat in vec4 vTone;
layout(location = 6) flat in vec4 vShadow;
layout(location = 7) flat in vec4 vClip;
layout(location = 8) flat in vec4 vExtra;

layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    vec4 uCanvas;   // w, h, ndc y sign, source y flip
    vec4 uMisc;
};
layout(binding = 1) uniform sampler2D uSrc;   // what is behind (mipmapped)

float sdShape(vec2 p, vec2 b, float r, float n) {
    vec2 q = abs(p) - b + r;
    vec2 m = max(q, 0.0);
    float c = (n <= 2.001) ? length(m) : pow(pow(m.x, n) + pow(m.y, n), 1.0 / n);
    return min(max(q.x, q.y), 0.0) + c - r;
}

vec3 tap(vec2 px, float lod) {
    vec2 uv = px / uCanvas.xy;
    if (uCanvas.w > 0.5)
        uv.y = 1.0 - uv.y;
    return textureLod(uSrc, uv, lod).rgb;
}

vec3 sampleBlur(vec2 px, float blur) {
    if (blur < 0.75)
        return tap(px, 0.0);
    float lod = max(0.0, log2(blur) - 1.0);
    vec3 acc = tap(px, lod) * 2.0;
    for (int i = 0; i < 8; ++i) {
        float a = 6.2831853 * (float(i) + 0.5) / 8.0;
        acc += tap(px + vec2(cos(a), sin(a)) * blur * 0.8, lod);
    }
    return acc / 10.0;
}

void main() {
    vec2 p = vPix;
    if (p.x < vClip.x || p.y < vClip.y || p.x > vClip.z || p.y > vClip.w)
        discard;

    vec2 b = vRect.zw * 0.5;
    vec2 lp = p - (vRect.xy + b);
    float r = min(vShape.x, min(b.x, b.y));
    float n = vExtra.x;
    float sd = sdShape(lp, b, r, n);

    // drop shadow, from the same distance field, offset down
    float shA = 0.0;
    if (vOptics.w > 0.0) {
        float ss = sdShape(lp - vec2(0.0, vShadow.y), b, r, n);
        float sg = max(vShadow.x * 0.5, 0.5);
        shA = vOptics.w * (1.0 - smoothstep(-sg, sg * 2.6, ss));
    }
    float cov = clamp(0.5 - sd, 0.0, 1.0);
    if (cov <= 0.0) {
        fragColor = vec4(0.0, 0.0, 0.0, shA);
        return;
    }

    // outward normal of the rim
    float e = 0.75;
    vec2 g = vec2(sdShape(lp + vec2(e, 0.0), b, r, n) - sdShape(lp - vec2(e, 0.0), b, r, n),
                  sdShape(lp + vec2(0.0, e), b, r, n) - sdShape(lp - vec2(0.0, e), b, r, n));
    vec2 nrm = dot(g, g) > 1e-10 ? normalize(g) : vec2(0.0);

    // the bevel: tilt from the squircle height profile, bend from Snell's law
    float d = max(-sd, 0.0);
    float bezel = max(vShape.y, 1.0);
    float x = clamp(d / bezel, 0.0, 1.0);
    float u = 1.0 - x;
    float u3 = u * u * u;
    float slope = u3 * pow(max(1.0 - u3 * u, 1e-5), -0.75);
    float tilt = atan(slope * vExtra.y);
    float bend = tan(tilt - asin(sin(tilt) / 1.5));      // 0 on the flat top, ~1.1 at a vertical rim
    vec2 disp = -nrm * bend * vShape.z;                   // toward the centre: edges magnify

    // whole-lens magnification (droplets), then the three wavelengths
    vec2 base = p - lp * vTone.w;
    float ca = vOptics.x;
    float blur = vShape.w;
    vec3 col;
    if (ca > 0.0005) {
        col.r = sampleBlur(base + disp * (1.0 - ca), blur).r;
        col.g = sampleBlur(base + disp, blur).g;
        col.b = sampleBlur(base + disp * (1.0 + ca), blur).b;
    } else {
        col = sampleBlur(base + disp, blur);
    }

    // vibrancy, legibility range, tint
    float luma = dot(col, vec3(0.2126, 0.7152, 0.0722));
    col = mix(vec3(luma), col, vTone.z);
    col = vTone.x + clamp(col, 0.0, 1.0) * (vTone.y - vTone.x);
    col = mix(col, vTint.rgb, vTint.a);
    col += vExtra.w;                                      // a uniform lift of the sheet

    // light: specular rim, bevel sheen, inner shadow
    vec2 L = vec2(cos(vOptics.z), -sin(vOptics.z));
    float facing = dot(nrm, L);
    float rim = exp(-d / max(vShadow.w, 0.35));
    float glint = pow(max(facing, 0.0), 2.0) + 0.55 * pow(max(-facing, 0.0), 2.0);
    float sheen = pow(sin(tilt), 3.0) * u;
    col += vOptics.y * (rim * (vExtra.z + (1.0 - vExtra.z) * glint) + 0.07 * sheen * glint);   // a faint catch-light, not a white outline
    float inner = vShadow.z * exp(-d / (bezel * 0.55)) * (0.5 - 0.5 * facing);
    col *= 1.0 - inner;
    col = clamp(col, 0.0, 1.0);

    fragColor = vec4(col, 1.0) * cov + vec4(0.0, 0.0, 0.0, shA) * (1.0 - cov);
}
