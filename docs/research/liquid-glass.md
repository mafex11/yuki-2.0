# Real Liquid Glass for the Yuki window

Research for making the window (`yuki/ui/window/`) actually refract — bend and magnify
what is behind it, with a specular highlight, chromatic fringing and true depth — instead
of the frosted/tinted panel it is today. No code changed; this is research only.

**Verdict up front.** The current `glass.py`/`theme.py` material (fill + linear-gradient
sheen + linear-gradient "specular edge" + blurred shadow, all flat `QPainter` gradients) is
exactly the "fake glassmorphism" the web community also produces by reflex — `backdrop-filter:
blur()` plus a white overlay and a border, with **no geometry-aware bending of the pixels
behind it.** Apple's material, and every real web recreation, differ from that in one
specific way: **the pixels behind the edge are displaced along the gradient of a rounded-rect
signed-distance field**, so a straight line crossing behind the glass visibly bends and a
strip of it visibly magnifies. That displacement is the one piece of physics the prototype is
missing, and it is add-able without adopting Qt Quick, without touching DWM, and without
capturing the desktop continuously. Recommended path: keep Qt Widgets; add a `QRhiWidget`
(or `QOpenGLWidget` if `QRhiWidget`'s Python bindings prove too thin) render surface for the
handful of glass shapes that deserve it (cards, sidebar, pills, the overlay), driven by a
hand-written GLSL fragment shader that computes the rounded-rect SDF **analytically per pixel**
— no displacement-map image needed, that's a workaround CSS/SVG needs and Qt doesn't — refracting
(a) the app's own content behind the shape, captured once as a texture and only re-captured when
that content actually changes, and (b) an optional once-per-session (or low-rate, opt-in) capture
of the real wallpaper. Full continuous desktop duplication is explicitly **not** recommended (see
§3).

---

## 1. Apple's Liquid Glass: what it actually is

Apple calls it "a new digital meta-material that dynamically bends and shapes light,"
introduced at WWDC 2025 (9 June 2025) and spanning iOS/iPadOS 26, macOS Tahoe, tvOS 26,
watchOS 26 and visionOS 26. Apple's own framing is explicitly against the old material:
prior translucency/vibrancy (iOS 7–18) *scattered* light into a soft diffuse blur; Liquid
Glass *lenses* it — "bends and concentrates light in real-time, the way curved glass
physically does." That is the one-sentence spec the current Yuki material fails: it scatters
(blurs) but never bends.

Apple's primary HIG page (`developer.apple.com/design/human-interface-guidelines/materials`)
and the technology overview page are JS-rendered SPAs that would not yield raw text to
`WebFetch`; the properties below are corroborated across WWDC25 session recaps
(wwdcnotes.com), independent technical write-ups, and the Apple-authored SwiftUI
implementation guide mirrored in `xcode-27-system-prompts`, cross-checked for consistency.

**Optical/interaction properties:**

- **Bends, doesn't just blur.** The signature behavior is lensing at the edges: content
  behind a curved edge visibly refracts, rather than only softening.
- **Specular highlights respond to motion.** "On interactions, Liquid Glass will flex and
  energize with light"; a highlight layer syncs to the device's gyroscope/accelerometer, so
  tilting the device visibly moves where light catches the glass. On a desktop with no
  gyroscope, the equivalent is: highlight position responds to cursor/window position, or to
  a synthetic light-sweep animation on interaction (press/hover), not a static gradient.
- **Adaptive tint from context.** "Light from colorful content nearby can spill onto its
  surface," and components can independently flip between light and dark rendering depending
  on what's actually behind them — the tint in the API is "a vibrant color that adapts to the
  content behind it," not a fixed alpha-white wash.
- **Depth scales with size.** Larger elements simulate a *thicker* sheet of glass: "deeper,
  richer shadows," "more pronounced lensing and refraction," "a softer scattering of light."
  Small controls (buttons, pills) behave like thin glass — sharp, high-curvature lensing at a
  tight radius; large panels behave like thick glass — softer, broader bending. This maps
  directly onto Yuki's existing `GlassLevel` tiers (`inset` < `regular` < `raised` < `lens`):
  they should differ in refraction *depth*, not just opacity, as they do today.
- **Shadows respond to content passing underneath** (more separation as content scrolls under
  a floating glass layer) — directly relevant to Yuki's Today timeline sitting under panels.
- **Morphing between shapes.** Adjacent glass elements can visually merge and split — e.g. a
  toolbar's separate buttons blending into one pill and back — via `GlassEffectContainer` +
  a shared `glassEffectID` namespace, animated under `withAnimation`. Not required for a first
  pass in Yuki; noted as a stretch goal for pills that group/ungroup.
- **Accessibility fallback.** The whole material explicitly degrades to opaque/static under
  Reduce Transparency, Increase Contrast and Reduce Motion — Yuki already has this instinct
  (`backdrop.py` checks `transparency_enabled()` and falls back to an opaque tinted gradient);
  the same switch should also disable the refraction shader, not just the DWM backdrop.
- **Usage discipline:** don't stack glass-on-glass; reserve tint for the one primary/emphasis
  element; glass is a *functional layer* (nav/controls) floating above a content layer, not a
  material for content itself.

**The actual rendering technique** (from independent technical analysis — Apple has not
published an implementation write-up): it is **2D displacement mapping, not 3D ray-traced
refraction**. From 1ar.io's reverse-engineering write-up: "a 2D displacement map where white
and black values determine the direction and distance pixels are shifted" — the interior
shrinks slightly, and pixels near the edge are pushed outward along the bezel's shape,
producing a lens-like bulge. "At that narrow viewing range, a 2D distortion is
indistinguishable from real 3D refraction." Exposed tuning is described as a "refraction
degree" and a "mask color." This is good news for a desktop implementation: it means Apple's
own material is the *same trick* the web SDF/GLSL implementations use (§2), just executed on
GPU compositor hardware — so the plan in §4 is not a simplification of Apple's approach, it
is the same approach.

**Developer API surface** (for parity/reference, not directly portable to Qt):

```swift
// SwiftUI
func glassEffect<S: Shape>(
    _ glass: Glass = .regular,          // .regular, .clear, .identity
    in shape: S = DefaultGlassEffectShape,
    isEnabled: Bool = true
) -> some View

// .glassEffect(.regular.tint(.orange).interactive())
// .buttonStyle(.glass) / .buttonStyle(.glassProminent)

// Morphing group:
GlassEffectContainer(spacing: CGFloat) {
    ViewA().glassEffectID("a", in: namespace)
    ViewB().glassEffectID("b", in: namespace)
}
```

UIKit exposes `UIGlassEffect` (via `UIVisualEffectView`) and a `UIGlassContainerEffect` as the
morphing analog of `GlassEffectContainer`. AppKit gets the same material in WWDC25 session
310 ("Build an AppKit app with the new design").

**Relevant WWDC25 sessions** (video pages are stub-only via fetch; recaps used instead):
219 "Meet Liquid Glass", 356 "Get to know the new design system", 220 "Say hello to the new
look of app icons", 323/284/310 "Build a SwiftUI/UIKit/AppKit app with the new design".

---

## 2. Real web refraction: exact math and code

Every non-fake browser implementation reduces to one SVG primitive:
**`feDisplacementMap`, fed by a generated displacement-map image via `feImage`**, applied
through `backdrop-filter` (Chromium only — Safari/Firefox mostly ignore SVG filters as
`backdrop-filter`, so these libraries fall back to plain blur there). A displacement map is
an RGBA image where **R encodes the X push and G encodes the Y push**, 0–255 with 128 =
no displacement; `feDisplacementMap`'s `scale` converts the normalized 0–255 range back to
pixels (0 → `-scale`, 128 → `0`, 255 → `+scale`).

```xml
<svg colorInterpolationFilters="sRGB">
  <filter id="liquidGlassFilter" filterUnits="userSpaceOnUse" x="0" y="0" width="W" height="H">
    <feImage href="<data-url-of-displacement-map>" x="0" y="0" width="W" height="H" result="displacement_map"/>
    <feDisplacementMap in="SourceGraphic" in2="displacement_map"
                        scale="SCALE" xChannelSelector="R" yChannelSelector="G"/>
  </filter>
</svg>
```
```css
.glass-panel { backdrop-filter: url(#liquidGlassFilter) blur(0.25px) contrast(1.2) brightness(1.05) saturate(1.1); }
```

**Fake vs. real, the actual test:** does a straight line passing behind the shape visibly
bend/magnify near the edge? Plain `backdrop-filter: blur(12px)` + `rgba(255,255,255,.15)` fill
+ `1px solid rgba(255,255,255,.3)` border — the standard "glassmorphism" Tailwind recipe, and
what Yuki's current `glass.py` does — answers no. That is the exact bug to fix.

### 2.1 The height-profile math (kube.io, "Liquid Glass in the Browser: Refraction with CSS and SVG")

Models the bezel as a literal surface-height function of normalized distance-from-edge
`x ∈ [0,1]`, then derives the refraction normal from its slope (a 2D Snell's-law
approximation, single refraction event, air→glass):

```
convex circle:    y = sqrt(1 - (1-x)^2)
convex squircle:  y = (1 - (1-x)^4) ^ (1/4)     // Apple's preferred profile — smoother
                                                  // flat -> curve transition, stays smooth
                                                  // when stretched into a rectangle
concave (bowl):   y = 1 - convex(x)
lip (rim + dip):  y = mix(convex(x), concave(x), smootherstep(x))
```

```javascript
// surface normal = derivative of the height field
const delta = 0.001;
const y1 = f(distanceFromSide - delta);
const y2 = f(distanceFromSide + delta);
const derivative = (y2 - y1) / (2 * delta);
const normal = { x: -derivative, y: 1 };

// normal -> displacement-map pixel (R,G centered on 128)
const result = { r: 128 + x * 127, g: 128 + y * 127, b: 128, a: 255 };
```
It precomputes displacement along one radius (127 samples) and rotates it symmetrically,
rather than evaluating every pixel by hand — a relevant trick if a precomputed texture route
is ever needed in Qt, though §4 argues Qt shouldn't need the texture indirection at all.

### 2.2 shuding/liquid-glass — the shortest complete implementation

The whole "shader" is a rounded-rect SDF plus a smoothstep, run per-pixel on a hidden canvas
(CPU, not GPU) to build the displacement-map image each frame:

```javascript
function smoothStep(a, b, t) {
  t = Math.max(0, Math.min(1, (t - a) / (b - a)));
  return t * t * (3 - 2 * t);
}
function length(x, y) { return Math.sqrt(x * x + y * y); }
function roundedRectSDF(x, y, width, height, radius) {
  const qx = Math.abs(x) - width + radius;
  const qy = Math.abs(y) - height + radius;
  return Math.min(Math.max(qx, qy), 0) + length(Math.max(qx, 0), Math.max(qy, 0)) - radius;
}

// "fragment shader", evaluated per output pixel in [0,1]^2 uv space
fragment: (uv, mouse) => {
  const ix = uv.x - 0.5, iy = uv.y - 0.5;
  const distanceToEdge = roundedRectSDF(ix, iy, 0.3, 0.2, 0.6);
  const displacement = smoothStep(0.8, 0, distanceToEdge - 0.15);   // refraction strength by
  const scaled = smoothStep(0, 1, displacement);                     // distance-to-edge
  return texture(ix * scaled + 0.5, iy * scaled + 0.5);              // pull toward center = lens
}
```
It then bakes that function into an actual displacement-map `<canvas>` image, normalizes the
largest (dx,dy) offset into a `maxScale`, writes the R/G bytes, and sets
`feDisplacementMap`'s `scale` from `maxScale`. No chromatic aberration or specular pass in
this minimal version — "specular" there is just `box-shadow` (outer drop shadow + inset
highlight), same trick Yuki's `paint_glass` already does with linear gradients.

### 2.3 rdev/liquid-glass-react — adds chromatic aberration and a GPU path

Filter chain: `feImage → feColorMatrix → feComponentTransfer → feDisplacementMap ×3 (one per
R/G/B channel, each a different scale) → feColorMatrix (isolate channel) → feBlend(screen) →
feGaussianBlur → feComposite (edge mask)`.

**Per-channel dispersion is literally a different displacement scale per color channel** —
this is the direct analog of a different index of refraction per wavelength:
```
R: displacementScale
G: displacementScale - aberrationIntensity * 0.05
B: displacementScale - aberrationIntensity * 0.10
```
recombined additively (`feBlend mode="screen"`) to get edge color fringing. Exposed
parameters: `displacementScale` (70 default), `blurAmount` (0.0625), `saturation` (140%),
`aberrationIntensity` (2), `elasticity` (0.15 — mouse-follow deformation within a 200px
activation radius), `cornerRadius`, and a `mode: "shader"` option that swaps the CPU canvas
loop for a **real WebGL fragment shader** (`ShaderDisplacementGenerator`) for quality/perf on
complex shapes — confirming the WebGL/GLSL route is the "serious" version of this same
technique, not a separate idea.

### 2.4 archisvaze/liquid-glass

Single-file demo with both an SVG-filter engine and a **switchable WebGL engine**, plus a
live control panel (refraction strength, frost blur, chromatic distortion, inner shadow,
tint, drag-to-reposition over arbitrary backgrounds) — useful mainly as confirmation that a
real-time GPU shader path is a first-class, shippable alternative to the canvas/SVG hack, not
just a toy.

### 2.5 A complete GLSL fragment shader (the GPU-native version of all of the above)

From a WebGL "refractive glass" write-up (zenn.dev/orectic) and Shadertoy SDF-refraction
demos, this is the per-pixel version that a real GPU shader (and therefore Qt) can do
directly — no displacement-map image indirection needed, because a fragment shader can
already read arbitrary neighboring texels analytically:

```glsl
// Signed distance field for a rounded rectangle, in shape-local pixel space
float sdRoundRect(vec2 p, vec2 halfSize, float r) {
    vec2 q = abs(p) - halfSize + r;
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}

// Surface normal via central-difference gradient of the SDF — this *is* the
// Snell's-law surface normal from §2.1, just computed analytically instead of baked.
float e = 1.5;
vec2 grad = vec2(
    sdRoundRect(p + vec2(e, 0.0), halfSize, radius) - sdRoundRect(p - vec2(e, 0.0), halfSize, radius),
    sdRoundRect(p + vec2(0.0, e), halfSize, radius) - sdRoundRect(p - vec2(0.0, e), halfSize, radius)
);
grad = normalize(grad + 1e-6);

// Refraction: displace the background sample along the normal, strongest near the edge
float curve = smoothstep(0.0, edgeWidth, -dist);      // 0 at the rim, 1 toward the center
float maxDisp = edgeWidth * 0.9;
vec2 disp = grad * (1.0 - curve) * uRefraction * maxDisp;
vec3 glassColor = sampleBlurred(fragPx + disp, uBlur);

// Chromatic aberration: separate R/G/B taps along the same normal, different magnitude
vec2 caOff = grad * (1.0 - curve) * uAberration * 14.0;
glassColor.r = sampleBlurred(fragPx + disp + caOff, uBlur).r;
glassColor.g = sampleBlurred(fragPx + disp,          uBlur).g;
glassColor.b = sampleBlurred(fragPx + disp - caOff,  uBlur).b;

// Specular rim: a directional highlight, brightest where the bent normal faces the light
vec3 n = normalize(vec3(grad * (1.0 - curve), 0.6));
vec3 lightDir = normalize(vec3(-0.6, 0.75, 0.55));
float spec = pow(max(dot(n, lightDir), 0.0), 12.0);
glassColor += spec * uSpecular * (1.0 - curve) * 1.4;
```

The key structural idea to carry into Qt: **one shared edge-proximity falloff curve
(`1.0 - curve`, or `smoothstep` on the SDF distance) drives refraction strength, chromatic
aberration magnitude, *and* specular intensity together** — all three effects are ~0 at the
shape's center and strongest at the rim, which is exactly the "lensing/refraction at the edges"
behavior specified in the task, "light bending across the whole curvature" being the smooth
gradient of that falloff, not a hard ring.

---

## 3. Doing this on Windows in PySide6 — options and verdict

Ground truth checked in this repo's own venv: **PySide6 6.11.2 / Qt 6.11.2**, `qsb.exe`
present at `PySide6/qsb.exe`, `QtQuick/Effects` (MultiEffect module) present,
`QtQuickWidgets` present, `QRhiWidget` present in `PySide6.QtWidgets`, `QOpenGLWidget`
present in `PySide6.QtOpenGLWidgets`.

### 3.1 Qt Quick `ShaderEffect` (+ `qsb`, + `MultiEffect`)

Pipeline: write GLSL 4.50 source → `qsb --glsl 100es,120,150 --hlsl 50 --msl 12 -o out.frag.qsb
in.frag` (or `qt6_add_shaders()` in CMake) bakes SPIR-V plus cross-compiled HLSL/MSL/GLSL
variants into one `.qsb` bundle → QML `ShaderEffect.fragmentShader` loads the `.qsb` → at
runtime `QRhi` picks the variant matching the active backend (Direct3D11/12 by default on
Windows, or Vulkan/OpenGL). `ShaderEffect` can sample **any QML item as a texture** via
`ShaderEffectSource` — including a whole subtree of siblings — so a hand-written fragment
shader reading "whatever is drawn behind this shape" as a `sampler2D` is directly supported;
that is the mechanism for self-refraction of the app's own layered content. `MultiEffect` is a
ready multi-pass shader (blur/shadow/mask/colorize/saturate) but exposes **no
displacement/refraction control** — useful only as the frosted-blur half of the material, not
the lensing half; Qt's own docs warn blur/shadow are the heaviest ops and to avoid animating
the source item so blur doesn't regenerate every frame, directly relevant to an
idle-when-static budget. No Qt-blessed refraction demo was found; nearest prior art is
third-party (§3.4).

### 3.2 `QRhiWidget`/`QOpenGLWidget` in Widgets, vs. `QQuickWidget`

`QRhiWidget` (Qt 6.7+) is the portable QRhi-backed successor to `QOpenGLWidget`: subclass it,
implement `initialize()`/`render()`, it owns a texture sized to the widget and composites
normally inside an ordinary `QWidget` tree — Qt's own Simple/Cube RHI Widget examples confirm
this, and it can load the exact same `.qsb`-baked shaders as `ShaderEffect` via
`QShader::fromSerialized`, so the shader code in §2.5/§4 is reusable either way.
`QQuickWidget` (embedding one QML panel inside the widget tree) spins up a full
`QQuickWindow`/scenegraph even for a single small panel — this project's own phase-1
measurement (`docs/UI-window.md`) already found Qt Quick added +33 MB open / +17 MB residual
for one simplified page versus Widgets, and chose Widgets specifically to avoid a second UI
stack. **`QRhiWidget` is the option that doesn't reopen that decision**: it stays inside the
already-chosen Widgets architecture and adds a GPU surface only where a shape actually needs
to refract.

Practical risk: `QRhiWidget`'s Python bindings are new (tech-preview class in Qt 6.7+, and
this repo is on 6.11.2 which does export it — confirmed above) — the binding's completeness
for the raw `QRhi`/`QRhiGraphicsPipeline` resource-building calls used in `initialize()`
should be prototyped early; `QOpenGLWidget` (present and mature in PySide6) is the fallback if
`QRhiWidget`'s Python surface turns out too thin, at the cost of only targeting the OpenGL
backend rather than being D3D/Vulkan-portable through QRhi.

### 3.3 Capturing the real desktop

Two Windows capture APIs: **DXGI Desktop Duplication** (`IDXGIOutputDuplication`, Win8+,
GPU-side, same-GPU-as-display, delivers a texture only on change — the historical OBS
"Display Capture" backend), and **Windows.Graphics.Capture / WGC** (Win10 1903+/Win11,
works cross-GPU, no elevation, the API Microsoft and OBS have migrated new capture code
toward). Python access exists via `winrt.windows.graphics.capture` +
`winrt.windows.graphics.capture.interop.create_for_monitor()/create_for_window()`; the
`winrt.windows.*` packages are **already installed in this project's venv**, so no new
dependency is needed if this route is used.

The blocker: **any use of WGC to capture the real desktop triggers Windows 11's
yellow/orange capture-indicator border**, permanent and unhideable unless the user has opted
into Settings → Privacy → "Screenshot borders" off (letting an app call
`SetIsBorderRequired(false)`). Running continuous desktop capture behind an always-open
companion window would put that border up essentially the whole time the window is open —
unacceptable for this use case. DWM itself is confirmed to give **no pixel access at all**:
Microsoft's System Backdrops/Mica docs describe Mica/Acrylic as a *named backdrop type*
(`DWM_SYSTEMBACKDROP_TYPE`) rendered inside the isolated `dwm.exe` compositor process; there
is no API to intercept or shader-process the pixels DWM composites behind a window. This
confirms `backdrop.py`'s own docstring ("DWM's work, done in the compositor: this process
pays nothing for it") — and also confirms DWM cannot be the source texture for a refraction
shader; only self-drawn content or an explicit capture can be.

### 3.4 Prior art and the verdict

Real prior art exists in exactly this stack: **`embark0420/PySide-Liquid-glass`** (GitHub) —
built on `QOpenGLWidget` with a "Shared FBO Texture Architecture," doing SDF-distance-based
radial edge displacement (`strength_x`/`strength_y` uniforms), 9-tap Gaussian-blurred
per-channel RGB chromatic aberration, and an `edge_highlight` specular term — i.e. almost
exactly the effect this report specifies, minus desktop capture (it refracts the app's own
FBO content only). `OverShifted/LiquidGlass` is a standalone GLSL liquid-glass shader worth
mining for displacement math. No evidence anywhere of a shipped app doing continuous
full-desktop-duplication-driven Liquid Glass on Windows; the closest continuous-capture
precedent (Wallpaper Engine, Rainmeter live-wallpaper plugins) runs one capture/composite per
frame on a dedicated render thread and is documented to add measurable GPU/battery cost —
not appropriate for a small always-open utility window.

**Recommendation:** self-refraction of the app's own layered content (rendered to an offscreen
texture, recaptured only when that content changes — the same FBO pattern as
`PySide-Liquid-glass`) plus a **static, low-rate wallpaper texture** (captured once at
startup/theme-change via a registry read or `SPI_GETDESKWALLPAPER`, *not* WGC, so no capture
border ever appears), with continuous WGC desktop duplication left as an explicit, off-by-default,
future opt-in — never the default path.

---

## 4. Implementation plan for Yuki

### 4.1 Where this plugs into the existing architecture

The prototype already separates *painting* (`glass.py`) from *material data* (`theme.py`)
from *native backdrop* (`backdrop.py`), and every glass shape is one of a small set of
`GlassLevel`s (`inset`, `regular`, `raised`, `lens`) drawn by a `Glass(QWidget)` base class
(`widgets.py`) via `paintEvent`. That separation is exactly right for adding a shader path
without a rewrite: **`Glass` gets an optional GPU-backed render surface; the theme keeps
owning the numbers.**

Concretely:

- Add a `GlassMaterial` dataclass (parallel to `GlassLevel`) holding shader uniforms:
  `refraction` (edge displacement strength, px), `edge_width` (bezel width, px — this *is*
  Apple's "depth": `inset`/`regular` get a narrow bezel and low refraction, `raised`/`lens`
  get a wider bezel and stronger lensing, per HIG's "larger = thicker glass" rule read the
  other way — Yuki's smallest, most-rounded shapes, the pills, should get the *tightest*
  radius and therefore the *strongest* per-pixel curvature, matching "thin glass, sharp
  lensing"), `chromatic` (aberration magnitude, px), `blur` (pre-blur radius on the sampled
  background, px), `specular` (rim intensity, 0–1), `specular_dir` (light vector, default
  top-left to match the existing sheen), `tint` (RGBA, reuses `theme.glass[level].fill`).
  Two instances per level, light and dark (dark needs lower `specular` and `blur` and a
  darker, less saturated `tint` — bright specular highlights read as gaudy on dark glass;
  this mirrors the existing `LIGHT`/`DARK` `GlassLevel` split in `theme.py`).
- Add `yuki/ui/window/refract.py`: a `QRhiWidget` subclass (`QOpenGLWidget` fallback) that
  is dropped in wherever `Glass`/`Panel`/`Pill`/`StatTile` currently paint themselves, taking
  a `GlassMaterial` and a source texture. For the many small, static, low-value surfaces
  (`inset` rows, hairline chips, the settings list) **keep plain `QPainter`** — refraction is
  only worth the GPU surface for shapes a user actually looks at: cards (`raised`), the
  sidebar panel (`regular`), the selected nav pill (`lens`), and the overlay/nudge/question
  cards and voice pill in `restyle.py`. This tiering keeps the GPU surface count small (single
  digits, not hundreds of widgets).
- Source texture, two layers, both cached and only re-rendered on change (never per frame):
  1. **App content behind the shape** — grab the widget stack under the glass rect once per
     invalidation (resize, page switch, scroll) into a `QImage`/GPU texture, the same
     invalidation triggers `_ShadowCache` already responds to in `glass.py`.
  2. **Wallpaper** — read `HKEY_CURRENT_USER\Control Panel\Desktop\WallPaper` (the pattern
     `backdrop.py` already uses for other registry reads) or call
     `SystemParametersInfoW(SPI_GETDESKWALLPAPER)`, decode once at startup and on
     `WM_SETTINGCHANGE`/theme change, never per frame, never via Windows.Graphics.Capture.
     This alone gets "the curved edges bend the real wallpaper" without ever raising the
     WGC capture border.
  3. *(Optional, off by default, phase-3+)* a low-rate (≤1–2 fps, or capture-on-move) WGC
     snapshot while the window is focused, explicitly opt-in, accepting the transient
     indicator — not part of the default build.

### 4.2 The shader itself

Single fragment shader serves every glass shape; per-instance uniforms come from
`GlassMaterial` plus the shape's own rect/radius. Written for `qsb` (input GLSL 4.40, the
form Qt's own shader examples and `qsb`'s SPIR-V front end expect):

```glsl
#version 440

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4  qt_Matrix;
    float qt_Opacity;
    vec2  uSize;         // shape size, px
    float uRadius;        // corner radius, px
    float uEdgeWidth;      // bezel width ("depth"), px
    float uRefraction;    // edge displacement strength, px
    float uChromatic;      // chromatic aberration magnitude, px
    float uBlur;          // background pre-blur radius, px
    float uSpecular;       // specular intensity, 0..1
    vec2  uLightDir;      // specular light direction, normalized
    vec4  uTint;           // straight-alpha tint (theme.glass[level].fill)
};

layout(binding = 1) uniform sampler2D uBackdrop;  // app content behind the shape (cached)

float sdRoundRect(vec2 p, vec2 halfSize, float r) {
    vec2 q = abs(p) - halfSize + r;
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}

vec3 sampleBackdrop(vec2 px) {
    // uBlur>0: a few offset taps stand in for a real gaussian; the backdrop texture is
    // small and cached, so a modest fixed tap count is cheap even per-pixel.
    vec2 uv = px / uSize;
    return texture(uBackdrop, uv).rgb;
}

void main() {
    vec2 p = (qt_TexCoord0 - 0.5) * uSize;         // shape-local px, origin at center
    vec2 halfSize = uSize * 0.5;
    float dist = sdRoundRect(p, halfSize, uRadius); // <0 inside, 0 at the rim, >0 outside

    float e = 1.5;
    vec2 grad = normalize(vec2(
        sdRoundRect(p + vec2(e, 0.0), halfSize, uRadius) - sdRoundRect(p - vec2(e, 0.0), halfSize, uRadius),
        sdRoundRect(p + vec2(0.0, e), halfSize, uRadius) - sdRoundRect(p - vec2(0.0, e), halfSize, uRadius)
    ) + 1e-6);

    // Edge-proximity falloff: 0 well inside the shape, 1 right at the rim — this one curve
    // drives refraction, chromatic aberration and specular together (see sec. 2.5).
    float edge = 1.0 - smoothstep(0.0, uEdgeWidth, -dist);

    vec2 fragPx = qt_TexCoord0 * uSize;
    vec2 disp   = grad * edge * uRefraction;
    vec2 caOff  = grad * edge * uChromatic;

    vec3 glassColor;
    glassColor.r = sampleBackdrop(fragPx + disp + caOff).r;
    glassColor.g = sampleBackdrop(fragPx + disp).g;
    glassColor.b = sampleBackdrop(fragPx + disp - caOff).b;

    // Tint: Apple's "vibrant color that adapts to the content behind it" — a straight alpha
    // blend of the theme fill over the refracted (not the raw) background.
    glassColor = mix(glassColor, uTint.rgb, uTint.a);

    // Specular rim, directional, strongest at the edge, matching the existing sheen's
    // top-left bias so the two techniques agree visually.
    vec3 n = normalize(vec3(grad * edge, 0.6));
    float spec = pow(max(dot(n, vec3(uLightDir, 0.55)), 0.0), 12.0);
    glassColor += spec * uSpecular * edge;

    float alpha = mix(uTint.a, 1.0, edge * 0.15);   // hairline definition right at the rim
    fragColor = vec4(glassColor, alpha) * qt_Opacity;
}
```

Build once at dev time, ship the compiled bytecode (matches Yuki's "no per-pixel Python,"
cache-what's-expensive style already in `glass.py`):

```
PySide6\qsb.exe --glsl 100es,120,150 --hlsl 50 --msl 12 -o glass.frag.qsb glass.frag
```

`QRhiWidget.initialize()` loads it with `QShader::fromSerialized(open("glass.frag.qsb").read())`
and builds one `QRhiGraphicsPipeline` per unique (level, theme) pair — four levels × two
themes = 8 pipelines, built once, reused for every instance of that level. `render()` only
re-issues the draw when the widget is dirty (resize, content invalidation, or a running
hover/press animation); otherwise it's skipped, which is the "idle when nothing changes" the
task asked for and matches the zero-cost-when-closed discipline already documented for the
rest of the window in `docs/UI-window.md`.

### 4.3 Light and dark variants

Keep the split `theme.py` already has (dark is not light inverted): dark-mode `GlassMaterial`
instances get lower `uSpecular` (bright highlights look artificial on dark glass), a smaller
`uChromatic` (less visually noisy against dark backgrounds), and a cooler/dimmer `uTint`
consistent with the existing `DARK` `GlassLevel.fill`/`sheen` values; light mode keeps the
brighter specular and the current sheen's warmth. Both should still expose the *same*
`uEdgeWidth`/`uRefraction` per level, since the geometry-bending behavior — not the
brightness — is the property actually missing today.

### 4.4 Layering by surface

- **Sidebar / panels (`regular`):** wide `uEdgeWidth`, moderate `uRefraction` (Apple's
  "thick glass, soft bending"); source = app content behind + wallpaper.
- **Cards (`raised`):** narrower bezel, stronger `uRefraction` and visible `uChromatic` at
  the corners; this is the shape users look at directly (Today's detail panel, Me's items),
  so it's worth the clearest lensing.
- **Pills (`lens`, nav pill, send button, voice pill):** small radius → high curvature by
  construction, so even a modest `uRefraction` reads as a strong lens; add a brief
  `uSpecular` pulse on press/hover as the desktop analog of SwiftUI's `.interactive()`
  "flex and energize with light."
- **Overlay / nudge / question cards (`restyle.py`):** same `raised` material; morphing
  (`GlassEffectContainer`-style merge/split between grouped pills) is explicitly deferred —
  static per-shape refraction already clears the bar the user set ("first attempt was just a
  frosted panel"); morphing is a phase-3 nicety, not required to fix the core complaint.

### 4.5 Performance budget

- GPU surfaces only on the handful of shapes above (single digits, not the 773 widgets the
  window can have open per `docs/UI-window.md`); everything else stays `QPainter`.
- Backdrop texture per surface is small (the shape's own rect, not the full window) and
  cached; re-render only on the same invalidations that already clear `_ShadowCache`.
- Wallpaper texture: one decode at startup/theme-change, never per frame, never via WGC by
  default — zero continuous GPU/CPU cost and no capture-indicator border.
- No `QQuickWidget`/scenegraph tax: staying on `QRhiWidget` inside the existing Widgets tree
  avoids reopening the Quick-vs-Widgets decision this repo already measured and settled
  (`docs/UI-window.md`: Quick added +33 MB open / +17 MB residual for one page).
  If `QRhiWidget`'s Python bindings prove too thin during prototyping, fall back to the
  mature `QOpenGLWidget` with the same shader (OpenGL-only, no D3D/Vulkan portability, but a
  known-working prior art exists at `embark0420/PySide-Liquid-glass`).
- Animated states (hover pulse, press flex) touch only the small pill/card surfaces already
  identified, for ~150–250 ms, matching the existing `MOTION` timings in `theme.py` — not a
  continuously-running shader.

---

## Sources

**Apple / Liquid Glass**
- https://en.wikipedia.org/wiki/Liquid_Glass
- https://developer.apple.com/videos/play/wwdc2025/219/ ("Meet Liquid Glass"; page stub only via fetch)
- https://wwdcnotes.com/documentation/wwdc25-219-meet-liquid-glass/
- https://1ar.io/updates/how-liquid-glass-works (2D displacement-map implementation account)
- https://joshcusick.substack.com/p/apples-liquid-glass-seemed-like-a-disaster-until-i-looked-closer
- https://developer.apple.com/documentation/swiftui/view/glasseffect(_:in:)
- https://developer.apple.com/documentation/swiftui/glass
- https://raw.githubusercontent.com/artemnovichkov/xcode-27-system-prompts/main/AdditionalDocumentation/SwiftUI-Implementing-Liquid-Glass-Design.md
- https://github.com/conorluddy/LiquidGlassReference
- https://developer.apple.com/forums/thread/791540 (`UIGlassContainerEffect`)
- https://developer.apple.com/documentation/technologyoverviews/liquid-glass (title-only via fetch)
- https://developer.apple.com/design/human-interface-guidelines/materials (title-only via fetch)

**Web refraction implementations**
- https://kube.io/blog/liquid-glass-css-svg/
- https://github.com/shuding/liquid-glass/blob/main/liquid-glass.js
- https://github.com/rdev/liquid-glass-react/blob/master/README.md
- https://github.com/rdev/liquid-glass-react/blob/master/src/index.tsx
- https://github.com/archisvaze/liquid-glass
- https://zenn.dev/orectic/articles/liquid-glass-webgl-refraction?locale=en
- https://www.shadertoy.com/view/wX3fDX
- https://github.com/w3c/svgwg/issues/1142 (interoperable backdrop displacement for liquid-glass UI)

**Qt / Windows rendering**
- https://doc.qt.io/qt-6/qml-qtquick-shadereffect.html
- https://doc.qt.io/qt-6/qtshadertools-overview.html
- https://doc.qt.io/qt-6/qtshadertools-build.html
- https://www.qt.io/blog/in-depth-custom-shader-effects
- https://www.qt.io/product/qt6/qml-book/ch10-effects-shader-elements
- https://doc.qt.io/qt-6/qml-qtquick-effects-multieffect.html
- https://www.qt.io/blog/qt-quick-multieffect
- https://www.qt.io/blog/a-short-guide-to-qt-quick-effects
- https://doc.qt.io/qt-6/qrhiwidget.html
- https://doc.qt.io/qt-6/qtwidgets-rhi-simplerhiwidget-example.html
- https://doc.qt.io/qt-6/qtwidgets-rhi-cuberhiwidget-example.html
- https://www.phoronix.com/news/Qt-6.7-Beta
- https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QRhiWidget.html
- https://doc.qt.io/qt-6/qtquick-performance.html
- https://forum.qt.io/topic/25330/memory-footprint-of-qml-applications
- https://python-winsdk.readthedocs.io/en/latest/api/windows/graphics.capture.interop.html
- https://pypi.org/project/winrt-Windows.Graphics.Capture/
- https://learn.microsoft.com/en-us/windows-hardware/drivers/display/desktop-duplication-api
- https://obsproject.com/forum/threads/windows-graphics-capture-vs-dxgi-desktop-duplication.149320/
- https://learn.microsoft.com/en-us/answers/questions/4172450/turn-off-the-yellow-screen-border-windows-11-uses
- https://learn.microsoft.com/en-us/windows/apps/develop/ui/system-backdrops
- https://learn.microsoft.com/en-us/windows/apps/design/style/mica
- https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type
- https://github.com/embark0420/PySide-Liquid-glass
- https://github.com/OverShifted/LiquidGlass
- https://bergice.github.io/liquidglass/

**Repo context read for §4** — `yuki/ui/window/glass.py`, `yuki/ui/window/theme.py`,
`yuki/ui/window/backdrop.py`, `yuki/ui/window/widgets.py`, `docs/UI-window.md`; local
environment check: PySide6 6.11.2 / Qt 6.11.2, `qsb.exe`, `QtQuick/Effects`,
`QtQuickWidgets`, `QRhiWidget` and `QOpenGLWidget` all present in the project venv.
