// Real browser qualification against an explicitly supplied development stack.
// Credentials stay in the private fixture; output contains checks and Run IDs.
import fs from 'node:fs/promises';
import {createHash} from 'node:crypto';
import { chromium } from '../node_modules/playwright/index.mjs';
const [fixturePath, output] = process.argv.slice(2);
if (!fixturePath || !output) throw Error('Supply private fixture and output file');
const fixture = JSON.parse(await fs.readFile(fixturePath, 'utf8'));
const base = new URL(fixture.base);
if (!['127.0.0.1', 'localhost'].includes(base.hostname)) throw Error('Development loopback required');
const browser = await chromium.launch({headless: true});
const checks = [], errors = [];
function check(name, value) { if (!value) throw Error(name); checks.push(name); }
async function api(context, path, options = {}) {
  const response = await context.request.fetch(base + 'dios/api/os/runtime/' + path, {maxRedirects: 0, ...options});
  return {status: response.status(), body: await response.json().catch(() => null)};
}
try {
  const context = await browser.newContext({locale: 'zh-CN', viewport: {width: 1440, height: 1000}});
  const page = await context.newPage(); page.on('pageerror', e => errors.push(e.message));
  await page.goto(base + 'dios/#console/tasks');
  await page.getByRole('link', {name: '企业登录', exact: true}).waitFor();
  check('enterprise-entry-no-token-input', await page.getByPlaceholder('Access Token').count() === 0);
  await page.getByRole('link', {name: '企业登录', exact: true}).click();
  await page.locator('#username').fill(fixture.username);
  await page.locator('#password').fill(fixture.password);
  await page.locator('#kc-login').click();
  await page.getByLabel('执行 Agent').waitFor({timeout: 30000});
  const first = await api(context, 'identity');
  check('native-login-to-dios-principal', first.status === 200 && first.body.mode === 'principal');
  check('automatic-source-intake', first.body.principal.upstream.ref.startsWith('enrolled/'));
  check('principal-navigation', await page.getByRole('button', {name: 'Models', exact: true}).count() === 0 && await page.getByRole('button', {name: 'Chat', exact: true}).count() === 0);
  check('no-browser-oauth-token-storage', await page.evaluate(() => !sessionStorage.getItem('dios_access_token') && !Object.values(localStorage).some(v => v.includes('access_token'))));
  const cookies = await context.cookies();
  check('native-httponly-session-ticket', cookies.some(c => c.name === '_oauth2_proxy' && c.httpOnly && c.sameSite === 'Lax' && c.value.length < 500));
  check('mixed-bearer-cookie-denied', (await api(context, 'identity', {headers: {Authorization: 'Bearer forged'}})).status === 400);
  const forged = await api(context, 'identity', {headers: {'X-Forwarded-Access-Token': 'forged', 'X-Auth-Request-User': 'admin'}});
  check('forwarded-identity-stripped', forged.status === 200 && forged.body.principal.id === first.body.principal.id);
  const writes = [
    ['missing-origin-denied', {'X-CSRF-Token': '1'}],
    ['foreign-origin-denied', {Origin: 'https://foreign.invalid', 'X-CSRF-Token': '1'}],
    ['null-origin-denied', {Origin: 'null', 'X-CSRF-Token': '1'}],
    ['same-site-not-same-origin-denied', {Origin: base.origin, 'X-CSRF-Token': '1', 'Sec-Fetch-Site': 'same-site'}],
    ['missing-custom-header-denied', {Origin: base.origin}],
  ];
  for (const [name, headers] of writes) check(name, (await api(context, 'runs', {method: 'POST', headers, data: {}})).status === 403);
  check('cors-preflight-denied', (await api(context, 'runs', {method: 'OPTIONS', headers: {Origin: 'https://foreign.invalid', 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'x-csrf-token'}})).status === 405);
  check('operator-routes-unavailable', (await context.request.get(base + 'dios/api/os/models')).status() === 404);
  check('internal-auth-unavailable', (await context.request.get(base + '_dios_auth')).status() === 404);
  check('logout-get-denied', (await context.request.get(base + 'oauth2/sign_out', {maxRedirects: 0})).status() === 405);
  check('logout-csrf-denied', (await context.request.post(base + 'oauth2/sign_out', {maxRedirects: 0})).status() === 403);
  const attacker = await context.newPage();
  await attacker.route('http://127.0.0.1:17655/**', route => route.fulfill({contentType: 'text/html', body: `<form method="POST" action="${base}dios/api/os/runtime/runs"><input name="goal" value="forged"></form>`}));
  await attacker.goto('http://127.0.0.1:17655/');
  const attacked = attacker.waitForResponse(r => r.url().startsWith(base + 'dios/api/'));
  await attacker.locator('form').evaluate(form => form.submit());
  const blocked = await attacked;
  check('real-browser-cross-origin-form-with-cookie-denied', blocked.status() === 403 && (await blocked.request().allHeaders()).cookie?.includes('_oauth2_proxy='));
  await attacker.close();
  if (fixture.api_base) {
    const direct = await context.request.get(fixture.api_base + '/api/os/runtime/identity', {headers: {'X-Forwarded-Access-Token': 'forged', 'X-Auth-Request-User': 'admin'}});
    check('direct-backend-cookie-and-forged-headers-denied', direct.status() === 401);
  }
  let run, submitted;
  if (fixture.run_flow) {
    await page.getByLabel('工作目标', {exact: true}).fill('企业登录、E2AG 审批与成果验证');
    const accepted = page.waitForResponse(r => r.url().endsWith('/api/os/runtime/runs') && r.request().method() === 'POST');
    await page.getByRole('button', {name: '发起任务', exact: true}).click();
    const response = await accepted; submitted = response.request().postDataJSON();
    check('browser-submission-with-csrf', response.status() === 202);
    run = await response.json();
    await fs.writeFile(output + '.pending.json', JSON.stringify({run_id: run.id, submitted, deadline: run.authorization.expires_at}));
    await page.getByRole('button', {name: '下载 Markdown 报告', exact: true}).waitFor({timeout: 90000});
    const final = await api(context, 'runs/' + run.id);
    check('real-worker-independent-approval-report', final.status === 200 && final.body.state === 'succeeded' && final.body.attempts.some(a => a.process_id && a.session_id));
    const artifact = final.body.artifacts.find(a => a.kind === 'result');
    const download = await context.request.get(base + `dios/api/os/runtime/runs/${run.id}/artifacts/${artifact.id}/download`);
    const bytes = await download.body();
    check('artifact-digest-and-citations', createHash('sha256').update(bytes).digest('hex') === artifact.digest && JSON.parse(bytes).citations.length > 0);
  }
  let refreshed;
  for (let attempt = 0; attempt < 20; attempt++) {
    const results = await Promise.all(Array.from({length: 6}, () => api(context, 'identity')));
    check('parallel-session-request-' + attempt, results.every(r => r.status === 200 && r.body.principal.id === first.body.principal.id));
    if (results.some(r => r.body.principal.upstream.ref !== first.body.principal.upstream.ref)) { refreshed = results.find(r => r.body.principal.upstream.ref !== first.body.principal.upstream.ref); break; }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  check('native-refresh-new-source', !!refreshed);
  if (run) {
    const responses = await page.evaluate(async body => Promise.all(Array.from({length: 6}, async () => {
      const response = await fetch('/dios/api/os/runtime/runs', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': '1'}, body: JSON.stringify(body)});
      return {status: response.status, body: await response.json()};
    })), submitted);
    check('refresh-retry-six-original-receipts', responses.every(r => r.status === 202 && r.body.id === run.id && r.body.authorization.reference === run.authorization.reference && r.body.authorization.expires_at === run.authorization.expires_at));
  }
  await page.screenshot({path: output + '.png', fullPage: true});
  await page.getByLabel('语言', {exact: true}).selectOption('en');
  check('english-session-controls', await page.getByRole('button', {name: 'Sign out', exact: true}).count() === 1);
  await page.getByLabel('Theme', {exact: true}).selectOption('dark');
  await page.getByLabel('Language', {exact: true}).selectOption('zh');
  await page.setViewportSize({width: 390, height: 844});
  check('mobile-layout', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  check('mobile-sign-out-visible', await page.getByRole('button', {name: '退出登录', exact: true}).evaluate(button => { const rect = button.getBoundingClientRect(); return rect.x >= 0 && rect.right <= innerWidth; }));
  await page.screenshot({path: output + '.mobile.png', fullPage: true});
  const savedCookies = await context.cookies();
  await page.getByRole('button', {name: '退出登录', exact: true}).click();
  await page.getByRole('link', {name: '企业登录', exact: true}).waitFor();
  check('ui-sign-out-clears-session', (await api(context, 'identity')).status === 401);
  const replay = await browser.newContext(); await replay.addCookies(savedCookies);
  check('old-ticket-replay-denied', (await api(replay, 'identity')).status === 401);
  await page.getByLabel('语言', {exact: true}).selectOption('en');
  check('english-login-entry', await page.getByRole('link', {name: 'Organization sign-in', exact: true}).count() === 1);
  check('no-page-errors', errors.length === 0);
  await fs.writeFile(output, JSON.stringify({checks, run_id: run?.id, firstSource: first.body.principal.upstream.ref, refreshedSource: refreshed.body.principal.upstream.ref, errors}, null, 2));
  console.log(JSON.stringify({checks, errors}));
} finally {await browser.close();}
