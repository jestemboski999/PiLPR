// Render docs/diagrams/architektura.svg do PNG z czcionką VT323 (przezroczyste tło),
// tło VHS dokłada diagram.py. Użycie: node diagram.js <wyjście.png>
const { chromium } = require('playwright');
const fs = require('fs'), path = require('path');
(async () => {
  const svg = fs.readFileSync(path.join(__dirname, '../diagrams/architektura.svg'), 'utf8');
  const font = fs.readFileSync('/home/admin/hailo-apps/local_resources/web/VT323-Regular.ttf').toString('base64');
  const html = `<style>@font-face{font-family:VT323;src:url(data:font/ttf;base64,${font})}
    html,body{margin:0;background:transparent}</style>${svg}`;
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1200, height: 780 }, deviceScaleFactor: 2 });
  await p.setContent(html);
  await p.evaluate(() => document.fonts.ready);
  await p.screenshot({ path: process.argv[2], omitBackground: true });
  await b.close();
})();
