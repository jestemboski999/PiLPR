// Render STL przez three.js (npm install three@0.160.0 w katalogu z render_stl.html), serwer: python -m http.server 8790
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({args:['--use-gl=swiftshader','--enable-unsafe-swiftshader']});
  const jobs = JSON.parse(process.argv[2]);
  for (const j of jobs) {
    const p = await b.newPage({ viewport: { width: 1200, height: 800 } });
    p.on('console', m => console.log(m.text()));
    await p.goto(`http://127.0.0.1:8790/render_stl.html?f=${j.f}&rx=${j.rx}&rz=${j.rz}`);
    await p.waitForFunction(() => document.title === 'done', null, { timeout: 60000 });
    await p.screenshot({ path: j.out, omitBackground: true });
  }
  await b.close();
})();
