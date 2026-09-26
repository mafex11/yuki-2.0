#version 440
// One instanced quad per glass shape. The quad covers the shape plus its shadow margin;
// every per-shape number arrives as a per-instance attribute (see refract.py: _pack).

layout(location = 0) in vec4 iRect;    // x, y, w, h                 (device px, canvas space)
layout(location = 1) in vec4 iShape;   // radius, bezel, height, blur
layout(location = 2) in vec4 iOptics;  // chroma, specular, light angle (rad), shadow alpha
layout(location = 3) in vec4 iTint;    // straight rgba
layout(location = 4) in vec4 iTone;    // tone min, tone max, saturation, magnify
layout(location = 5) in vec4 iShadow;  // shadow blur, shadow y, inner shadow, rim width
layout(location = 6) in vec4 iClip;    // x0, y0, x1, y1
layout(location = 7) in vec4 iExtra;   // corner exponent, curvature, rim light, lift

layout(location = 0) out vec2 vPix;
layout(location = 1) flat out vec4 vRect;
layout(location = 2) flat out vec4 vShape;
layout(location = 3) flat out vec4 vOptics;
layout(location = 4) flat out vec4 vTint;
layout(location = 5) flat out vec4 vTone;
layout(location = 6) flat out vec4 vShadow;
layout(location = 7) flat out vec4 vClip;
layout(location = 8) flat out vec4 vExtra;

layout(std140, binding = 0) uniform buf {
    vec4 uCanvas;   // w, h, ndc y sign, source y flip
    vec4 uMisc;
};

void main() {
    float m = iShadow.x * 2.0 + abs(iShadow.y) + 2.0;
    vec2 corner = vec2(float(gl_VertexIndex & 1), float((gl_VertexIndex >> 1) & 1));
    vec2 p = iRect.xy - m + corner * (iRect.zw + 2.0 * m);
    vPix = p;
    vRect = iRect; vShape = iShape; vOptics = iOptics; vTint = iTint;
    vTone = iTone; vShadow = iShadow; vClip = iClip; vExtra = iExtra;
    vec2 ndc = p / uCanvas.xy * 2.0 - 1.0;
    gl_Position = vec4(ndc.x, ndc.y * uCanvas.z, 0.0, 1.0);
}
