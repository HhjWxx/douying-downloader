import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const directory = path.resolve(process.env.DOUYIN_RELEASE_DIR || 'release');
const lines = [];
for (const filename of (await readdir(directory)).sort()) {
  if (!/\.(dmg|exe)$/.test(filename)) continue;
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path.join(directory, filename))) hash.update(chunk);
  lines.push(`${hash.digest('hex')}  ${filename}`);
}
await writeFile(path.join(directory, 'SHA256SUMS.txt'), lines.join('\n') + '\n');
console.log(`安装包校验值已写入 ${path.join(directory, 'SHA256SUMS.txt')}`);
