#version 440
// A full-canvas triangle; uv (0,0) is the top-left pixel.

layout(location = 0) out vec2 vUV;

layout(std140, binding = 0) uniform buf {
    vec4 uCanvas;   // w, h, ndc y sign, source y flip
    vec4 uMisc;
};

void main() {
    vec2 pos = vec2(float((gl_VertexIndex << 1) & 2), float(gl_VertexIndex & 2));
    vUV = pos;
    vec2 ndc = pos * 2.0 - 1.0;
    gl_Position = vec4(ndc.x, ndc.y * uCanvas.z, 0.0, 1.0);
}
