// Regenerate native icons and web favicon from packaging/prism-browser.svg after `npm ci --prefix web`.
import { readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";

// Next's installed sharp dependency provides the SVG renderer on all build hosts.
const require = createRequire(new URL("../web/package.json", import.meta.url));
const sharp = require("sharp");
const asset = (name) => new URL(`../packaging/prism-browser.${name}`, import.meta.url);
const faviconPath = new URL("../web/src/app/favicon.ico", import.meta.url);

const svg = await readFile(asset("svg"));
const sizes = [16, 24, 32, 48, 64, 128, 256, 512, 1024];
const images = new Map(await Promise.all(sizes.map(async (size) => [
  // Rasterize the 128-unit SVG at 1024px (density 576) before scaling down.
  size, await sharp(svg, { density: 576 }).resize(size, size).png().toBuffer(),
])));
await writeFile(asset("png"), images.get(1024));

// ICNS PNG representations, including the Retina variants.
const representations = [
  ["icp4", 16], ["icp5", 32], ["icp6", 64], ["ic07", 128],
  ["ic08", 256], ["ic09", 512], ["ic10", 1024],
  ["ic11", 32], ["ic12", 64], ["ic13", 256], ["ic14", 512],
];
const chunks = representations.map(([type, size]) => {
  const header = Buffer.alloc(8);
  header.write(type);
  header.writeUInt32BE(images.get(size).length + 8, 4);
  return Buffer.concat([header, images.get(size)]);
});
const header = Buffer.alloc(8);
header.write("icns");
header.writeUInt32BE(8 + chunks.reduce((sum, chunk) => sum + chunk.length, 0), 4);
await writeFile(asset("icns"), Buffer.concat([header, ...chunks]));

// Windows ICO + web favicon (PNG images inside ICO directory, Vista and later).
const windowsSizes = sizes.filter((size) => size <= 256);
const directory = Buffer.alloc(6 + windowsSizes.length * 16);
directory.writeUInt16LE(1, 2);
directory.writeUInt16LE(windowsSizes.length, 4);
let offset = directory.length;
windowsSizes.forEach((size, index) => {
  const entry = 6 + index * 16;
  const image = images.get(size);
  directory[entry] = directory[entry + 1] = size === 256 ? 0 : size;
  directory.writeUInt16LE(1, entry + 4);
  directory.writeUInt16LE(32, entry + 6);
  directory.writeUInt32LE(image.length, entry + 8);
  directory.writeUInt32LE(offset, entry + 12);
  offset += image.length;
});
const icoBuffer = Buffer.concat([directory, ...windowsSizes.map((size) => images.get(size))]);
await writeFile(asset("ico"), icoBuffer);
await writeFile(faviconPath, icoBuffer);
console.log("Generated PNG, ICNS, ICO and web/src/app/favicon.ico from packaging/prism-browser.svg");
