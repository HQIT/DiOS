// Explicit development qualification; the external driver controls IdP outage/approval.
import fs from 'node:fs/promises';
import {chromium} from '../node_modules/playwright/index.mjs';
const [fixturePath, output] = process.argv.slice(2);
const fixture = JSON.parse(await fs.readFile(fixturePath, 'utf8'));
const base = new URL(fixture.base);
if (!['localhost', '127.0.0.1'].includes(base.hostname)) throw Error('Loopback fixture required');
const browser = await chromium.launch({headless: true});
const checks = [], errors = [];
function check(name, value) { if (!value) throw Error(name); checks.push(name); }
async function waitFile(suffix) {
  for (let i = 0; i < 180; i++) {
    if (await fs.stat(output + suffix).catch(() => null)) return;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw Error('Driver did not reach ' + suffix);
}
async function submit(name) {
  const context = await browser.newContext({locale: 'zh-CN', viewport: {width: 1440, height: 1000}});
  const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + 'dios/#console/tasks');
  await page.getByRole('link', {name: '企业登录', exact: true}).click();
  await page.locator('#username').fill(fixture.username);
  await page.locator('#password').fill(fixture.password);
  await page.locator('#kc-login').click();
  await page.getByLabel('工作目标', {exact: true}).fill(name);
  const response = page.waitForResponse(r => r.url().endsWith('/api/os/runtime/runs') && r.request().method() === 'POST');
  await page.getByRole('button', {name: '发起任务', exact: true}).click();
  const result = await response;
  check('submitted-' + name, result.status() === 202);
  return {context, page, run: await result.json()};
}
try {
  const a = await submit('身份对账：独立会话 A');
  const b = await submit('身份对账：独立会话 B');
  await fs.writeFile(output + '.pending.json', JSON.stringify({a: a.run, b: b.run}));
  await waitFile('.logout');
  await a.page.getByRole('button', {name: '退出登录', exact: true}).click();
  await a.page.getByRole('link', {name: '企业登录', exact: true}).waitFor();
  await fs.writeFile(output + '.logged-out', 'true');
  await b.page.getByRole('button', {name: '下载 Markdown 报告', exact: true}).waitFor({timeout: 150000});
  check('peer-source-check-visible', await b.page.getByTestId('source-check').innerText().then(t => t.includes('来源有效')));
  await b.page.getByTestId('source-check').scrollIntoViewIfNeeded();
  await b.page.screenshot({path: output + '.png', fullPage: true});
  await b.page.getByLabel('语言', {exact: true}).selectOption('en');
  const translated = await b.page.getByTestId('source-check').innerText();
  check('english-source-status', !/[\u4e00-\u9fff]/.test(translated));
  await b.page.setViewportSize({width: 390, height: 844});
  await b.page.getByTestId('source-check').scrollIntoViewIfNeeded();
  check('mobile-layout', await b.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await b.page.screenshot({path: output + '.mobile.png', fullPage: true});
  check('no-page-errors', errors.length === 0);
  await fs.writeFile(output, JSON.stringify({checks, errors, runs: {a: a.run.id, b: b.run.id}}, null, 2));
  await b.page.getByRole('button', {name: 'Sign out', exact: true}).click();
  console.log(JSON.stringify({checks, errors}));
} finally { await browser.close(); }
