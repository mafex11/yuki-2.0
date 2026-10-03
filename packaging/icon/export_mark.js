// Export one Yuki logo mark from the logo gallery's own dot code (docs/ui-mockups/revamp-2026-10/logo: dots.js and
// marks-*.js, the same code the gallery draws with) as one SVG per icon size: the hand-tuned round-dot lists at 48 px
// and under, the sampled dot field at 64 and 256 px. Run by packaging/make_icon.py --export; needs Node only.
//
//   node export_mark.js --logo <gallery dir> --mark <id or number, e.g. planet / 06a> --ramp <mint|night|bang> --out <dir>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function arg(name, fallback) {
  const i = process.argv.indexOf('--' + name);
  return i > 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
}
const logo = arg('logo', '');
const want = arg('mark', '');
const ramp = arg('ramp', 'mint');
const out = arg('out', '');
const sizes = (arg('sizes', '16,20,24,32,40,48,64,256')).split(',').map(Number);
if (!logo || !want || !out) { console.error('usage: --logo DIR --mark ID --ramp RAMP --out DIR'); process.exit(2); }

// the gallery runs in a browser: give it the little it reads (no page, no canvas is drawn for SVG output)
const sandbox = { console, Math, Float32Array, Uint8Array, Array, Object, String, Number, JSON, URLSearchParams,
  location: { search: '?ramp=' + encodeURIComponent(ramp) }, devicePixelRatio: 1 };
sandbox.window = sandbox;
vm.createContext(sandbox);
for (const f of ['dots.js', 'marks-planet.js', 'marks-letters.js', 'marks-classic.js']) {
  const file = path.join(logo, f);
  if (!fs.existsSync(file)) continue;
  vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: f });
}
const D = sandbox.YukiDots;
const marks = sandbox.YukiMarks || [];
if (!D) { console.error('dots.js not found in ' + logo); process.exit(3); }
if (!D.RAMPS[ramp]) { console.error('unknown colour set: ' + ramp + ' (have ' + Object.keys(D.RAMPS).join(', ') + ')'); process.exit(4); }
D.state.ramp = ramp;
const def = marks.find((m) => m.id === want || m.no === want);
if (!def) { console.error('unknown mark: ' + want + ' (have ' + marks.map((m) => m.no + ' ' + m.id).join(', ') + ')'); process.exit(5); }
fs.mkdirSync(out, { recursive: true });
for (const s of sizes) {
  let svg = D.iconSVG(def, s);
  if (!svg.includes('xmlns=')) svg = svg.replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" ');
  fs.writeFileSync(path.join(out, s + '.svg'), svg + '\n');
}
fs.writeFileSync(path.join(out, 'mark.json'), JSON.stringify({ id: def.id, no: def.no, name: def.name, ramp, sizes }, null, 1) + '\n');
console.log('exported ' + def.no + ' ' + def.id + ' (' + ramp + ') to ' + out);
