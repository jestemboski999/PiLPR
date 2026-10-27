const { chromium } = require('playwright');
const OUT = require('path').join(__dirname, '..', 'images') + '/';
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  p.on('pageerror', e => console.log('ERR', e.message));
  await p.goto('http://127.0.0.1:8765/', { waitUntil: 'networkidle' });
  await p.waitForTimeout(2000);
  await p.screenshot({ path: OUT + 'www_panel.png' });
  const modals = [
    ['openWlModal()', 'closeWlModal()', 'www_bazy.png'],
    ['openRadarModal()', 'closeRadarModal()', 'www_radar.png'],
    ['openRecModal()', 'closeRecModal()', 'www_nagrania.png'],
    ['reviewerOpen()', 'reviewerClose()', 'www_edytor.png'],
  ];
  for (const [fn, close, name] of modals) {
    try {
      await p.evaluate(fn);
      await p.waitForTimeout(2000);
      await p.screenshot({ path: OUT + name });
      await p.evaluate(close);
      await p.waitForTimeout(500);
    } catch (e) { console.log(name, e.message); }
  }
  const m = await b.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await m.goto('http://127.0.0.1:8765/', { waitUntil: 'networkidle' });
  await m.waitForTimeout(2000);
  await m.screenshot({ path: OUT + 'www_telefon.png' });
  await b.close();
})();
