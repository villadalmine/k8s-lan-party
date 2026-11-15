// Optional integration check, run on the operator's machine through an SSH tunnel.
// Requires Playwright in Node's module search path; it is never served or needed by the console.
// node poc/console/check-browser.cjs <private-url-file> [unknown|open|closed]
const fs = require('fs');
const assert = require('assert/strict');
const { chromium } = require('playwright');

(async () => {
  const url = fs.readFileSync(process.argv[2], 'utf8').trim();
  const expected = process.argv[3] || 'unknown';
  assert(['unknown', 'open', 'closed'].includes(expected), 'expected page state must be unknown, open or closed');
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    // Delay a real response, preserving its body: changing tabs while initial status loads must
    // neither replace the selected view nor leave the header saying "loading" indefinitely.
    await page.route('**/api/v1/status', async (route) => {
      const response = await route.fetch();
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await route.fulfill({ response });
    });
    await page.goto(url);
    await page.getByRole('button', { name: 'Public page', exact: true }).click();
    const view = page.locator('#view');
    await view.getByText('Last observed state: ' + expected, { exact: true }).waitFor();
    assert.notEqual(await page.locator('#summary').innerText(), 'loading', 'a loaded view must settle the header');
    assert((await view.innerText()).includes('not a live check'));
    if (expected !== 'unknown') assert((await view.innerText()).includes('Observed at'));
    await page.waitForTimeout(1700);  // the delayed status has arrived: Public page must still be selected
    assert.equal(await view.locator('h2').innerText(), 'Public page');
    assert(!page.url().includes('#t='), 'token must leave the address bar');
    await page.getByRole('button', { name: 'Pool', exact: true }).click();
    await view.getByRole('heading', { name: 'Active leases', exact: true }).waitFor();
    assert((await view.innerText()).includes('slots_total'));
    await page.getByRole('button', { name: 'Metrics', exact: true }).click();
    await view.locator('pre').waitFor();
    assert((await view.locator('pre').innerText()).includes('emesh_admission_'));
    const disabled = page.locator('#hostverbs button');
    assert(await disabled.count() >= 5);
    for (let i = 0; i < await disabled.count(); i++) assert(await disabled.nth(i).isDisabled());
    assert.deepEqual(errors, [], 'no browser JavaScript errors');
    console.log('BROWSER PASS: rapid tab switch, header, Pool, Public page (' + expected + '), Metrics, disabled verbs, token removed');
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e.message.replace(/#t=[A-Za-z0-9_-]+/g, '#t=<redacted>'));
  process.exitCode = 1;
});
