import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const root = path.resolve(__dirname, '..'); // web/scripts -> web
const pkgPath = path.join(root, 'package.json');
const pubDir = path.join(root, 'public');
const tplPath = path.join(pubDir, 'sw.template.js');
const outPath = path.join(pubDir, 'sw.js');

const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf-8'));
const version = pkg.version || '0.0.0';

const ts = new Date().toISOString().replace(/[-:TZ.]/g, ''); // yyyymmddhhmmssmmm

function tryGit(cmd) {
  try {
    return execSync(cmd, { cwd: root, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
  } catch {
    return '';
  }
}

// Opcional: tomar hash de Git si existe, y marcar "-dirty" si hay cambios no commiteados
const gitHash = tryGit('git rev-parse --short HEAD');
const isDirty = tryGit('git status --porcelain');
const hashPart = gitHash ? `${gitHash}${isDirty ? '-dirty' : ''}-` : '';

const buildId = `${version}-${hashPart}${ts}`;

if (!fs.existsSync(tplPath)) {
  console.error('No existe', tplPath);
  process.exit(1);
}

const tpl = fs.readFileSync(tplPath, 'utf-8');
const out = tpl.replace(/__BUILD_ID__/g, buildId);
fs.writeFileSync(outPath, out, 'utf-8');

console.log(`SW generado: CACHE_NAME = nexora-app-cache-${buildId}`);
