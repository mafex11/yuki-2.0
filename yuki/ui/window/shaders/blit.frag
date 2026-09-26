#version 440
// Draws a premultiplied texture over the target (blend: one, one-minus-source-alpha).

layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    vec4 uCanvas;   // w, h, ndc y sign, source y flip
    vec4 uMisc;
};
layout(binding = 1) uniform sampler2D uSrc;

void main() {
    vec2 uv = vUV;
    if (uCanvas.w > 0.5)
        uv.y = 1.0 - uv.y;
    fragColor = textureLod(uSrc, uv, 0.0);
}
