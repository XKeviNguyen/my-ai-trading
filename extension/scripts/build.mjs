// Assemble the unpacked extension in dist/: compiled JS from build/src, static files, manifest and
// generated PNG icons. Node built-ins only (no bundler, no extra dependencies).
import { cpSync, mkdirSync, readdirSync, rmSync, statSync, writeFileSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = join(root, "build", "src");
const dist = join(root, "dist");

rmSync(dist, { recursive: true, force: true });
mkdirSync(join(dist, "icons"), { recursive: true });

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

let jsCount = 0;
for (const file of walk(src)) {
  if (!file.endsWith(".js")) continue;
  const out = join(dist, relative(src, file));
  mkdirSync(dirname(out), { recursive: true });
  cpSync(file, out);
  jsCount += 1;
}
for (const name of readdirSync(join(root, "static"))) cpSync(join(root, "static", name), join(dist, name), { recursive: true });
// Re-serialise the manifest to guarantee it is valid JSON.
writeFileSync(join(dist, "manifest.json"), JSON.stringify(JSON.parse(readFileSync(join(root, "manifest.json"), "utf8")), null, 2));

// ---- minimal PNG writer for solid icons with a simple bar-chart glyph ----
const crcTable = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}
function icon(size) {
  const bg = [26, 95, 180, 255];
  const fg = [255, 255, 255, 255];
  const bars = [0.35, 0.6, 0.45, 0.8]; // relative heights
  const rows = [];
  for (let y = 0; y < size; y++) {
    const row = [0]; // filter: none
    for (let x = 0; x < size; x++) {
      const pad = size * 0.18;
      const inner = size - 2 * pad;
      const slot = inner / bars.length;
      const i = Math.floor((x - pad) / slot);
      const inBarX = x >= pad && x < size - pad && (x - pad) % slot < slot * 0.62;
      const h = i >= 0 && i < bars.length ? bars[i] * inner : 0;
      const inBarY = y >= size - pad - h && y < size - pad;
      row.push(...(inBarX && inBarY ? fg : bg));
    }
    rows.push(Buffer.from(row));
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(Buffer.concat(rows))),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}
for (const s of [16, 48, 128]) writeFileSync(join(dist, "icons", `icon${s}.png`), icon(s));

console.log(`dist/: ${jsCount} JS modules, static files, manifest.json, 3 icons`);
