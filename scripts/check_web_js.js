// Syntax check of the Lattice window's scripts (node scripts/check_web_js.js): parses each file, runs nothing.
const fs = require('fs');
const path = require('path');
const root = path.join(__dirname, '..', 'yuki', 'ui', 'web');
const files = ['lattice/field.js', 'bridge.js', 'ui.js', 'app.js', 'fixtures/window.js']
  .concat(fs.readdirSync(path.join(root, 'pages')).filter((f) => f.endsWith('.js')).map((f) => 'pages/' + f));
let bad = 0;
for (const f of files) {
  try { new Function(fs.readFileSync(path.join(root, f), 'utf8')); console.log('ok   ' + f); }
  catch (e) { bad++; console.log('FAIL ' + f + ': ' + e.message); }
}
process.exit(bad ? 1 : 0);
