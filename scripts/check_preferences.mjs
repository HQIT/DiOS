import fs from 'node:fs/promises';
const {chromium}=await import(new URL('../node_modules/playwright/index.mjs',import.meta.url));
const dir=process.argv[2];const tokens=JSON.parse(await fs.readFile(dir+'/tokens.json','utf8'));
const browser=await chromium.launch({headless:true});const checks=[];const errors=[];
function assert(ok,message){if(!ok)throw new Error(message);checks.push(message);}
try {
 const context=await browser.newContext({locale:'en-US',colorScheme:'light',viewport:{width:1440,height:1000}});
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:13082/dios/#console/tasks');
 await page.getByText('Enter your Access Token to continue',{exact:true}).waitFor();
 assert(await page.getByLabel('Theme',{exact:true}).inputValue()==='auto','Theme defaults to Auto');
 assert(await page.getByLabel('Language',{exact:true}).inputValue()==='auto','Language defaults to browser');
 assert(await page.locator('html').getAttribute('lang')==='en','English browser selects English');
 await page.getByPlaceholder('Access Token').fill('draft-credential');
 await page.getByLabel('Language',{exact:true}).selectOption('zh');
 await page.getByText('请输入 Access Token 以继续',{exact:true}).waitFor();
 assert(await page.getByPlaceholder('Access Token').inputValue()==='draft-credential','Language switch preserves form input');
 await page.getByLabel('主题',{exact:true}).selectOption('dark');
 await page.emulateMedia({colorScheme:'light'});
 assert(await page.locator('html').getAttribute('data-theme')==='dark','Manual theme overrides OS');
 await page.reload();await page.getByLabel('主题',{exact:true}).waitFor();
 assert(await page.getByLabel('主题',{exact:true}).inputValue()==='dark','Theme preference persists');
 assert(await page.getByLabel('语言',{exact:true}).inputValue()==='zh','Language preference persists');
 await page.getByLabel('主题',{exact:true}).selectOption('auto');await page.emulateMedia({colorScheme:'light'});
 assert(await page.locator('html').getAttribute('data-theme')==='light','Auto follows light OS');
 await page.emulateMedia({colorScheme:'dark'});await page.waitForFunction(()=>document.documentElement.dataset.theme==='dark');
 assert(await page.locator('html').getAttribute('data-theme')==='dark','Auto follows live OS changes');
 await page.getByLabel('语言',{exact:true}).selectOption('auto');
 await page.getByText('Enter your Access Token to continue',{exact:true}).waitFor();
 await page.evaluate(token=>sessionStorage.setItem('dios_access_token',token),tokens.operator);await page.reload();
 await page.getByText('Delegate work. Receive results.',{exact:true}).waitFor();
 await page.getByLabel('Work objective',{exact:true}).fill('用户草稿 User draft — 保持原样');
 await page.getByLabel('Language',{exact:true}).selectOption('zh');
 assert(await page.getByLabel('工作目标',{exact:true}).inputValue()==='用户草稿 User draft — 保持原样','Task draft retained across language change');
 await page.getByLabel('语言',{exact:true}).selectOption('en');
 for(const mode of ['light','dark']){
  await page.getByLabel('Theme',{exact:true}).selectOption(mode);
  for(const route of ['console/tasks','console/registry/mcp','console/registry/skills','console/registry/plugins','workbench/employee/overview','workbench/admin/operations']){
   await page.goto('http://127.0.0.1:13082/dios/#'+route);await page.getByLabel('Theme',{exact:true}).waitFor();await page.waitForTimeout(250);
   assert(await page.locator('html').getAttribute('data-theme')===mode,mode+' '+route);
   if(route.endsWith('overview')||route==='console/tasks')await page.screenshot({path:dir+'/'+mode+'-'+(route.startsWith('workbench')?'workbench':'tasks')+'.png',fullPage:true});
  }
 }
 await page.setViewportSize({width:390,height:844});
 for(const route of ['workbench/employee/overview','console/tasks']){
  await page.goto('http://127.0.0.1:13082/dios/#'+route);await page.getByLabel('Theme',{exact:true}).waitFor();
  await page.getByLabel('Language',{exact:true}).selectOption('zh');await page.getByLabel('语言',{exact:true}).selectOption('en');
  assert(!(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)), 'Mobile fits '+route);
  await page.screenshot({path:dir+'/preferences-mobile-'+route.split('/')[0]+'.png',fullPage:true});
 }
 await page.setViewportSize({width:1440,height:1000});
 await page.goto('http://127.0.0.1:13082/dios/#console/agents');
 await page.getByText('Add Agent',{exact:true}).click();
 await page.getByPlaceholder('e.g. code-reviewer',{exact:true}).fill('保留 Agent draft');
 // The drawer covers the header; preference changes from another tab must retain its form.
 const controls=await context.newPage();await controls.goto('http://127.0.0.1:13082/dios/#workbench');
 await controls.getByLabel('Language',{exact:true}).selectOption('zh');
 await page.getByPlaceholder('例如：code-reviewer',{exact:true}).waitFor();
 assert(await page.getByPlaceholder('例如：code-reviewer',{exact:true}).inputValue()==='保留 Agent draft','Drawer switches language without losing input');
 await controls.getByLabel('语言',{exact:true}).selectOption('en');await controls.close();
 const blocked=await browser.newContext({locale:'zh-TW',colorScheme:'light'});
 await blocked.addInitScript(()=>Object.defineProperty(Storage.prototype,'getItem',{value(){throw new Error('Blocked storage');}}));
 const bp=await blocked.newPage();await bp.goto('http://127.0.0.1:13082/dios/#workbench');await bp.getByLabel('主题',{exact:true}).waitFor();
 assert(await bp.locator('html').getAttribute('data-theme')==='light','Blocked storage falls back to Auto');
 await bp.getByLabel('主题',{exact:true}).selectOption('dark');assert(await bp.locator('html').getAttribute('data-theme')==='dark','Theme works with unavailable storage');
 await bp.getByLabel('语言',{exact:true}).selectOption('en');await bp.getByLabel('Language',{exact:true}).waitFor();checks.push('Language works with unavailable storage');
 await blocked.close();
 const zh=await browser.newContext({locale:'zh-CN',colorScheme:'dark'});const z=await zh.newPage();await z.goto('http://127.0.0.1:13082/dios/#workbench');await z.getByLabel('语言',{exact:true}).waitFor();
 assert(await z.locator('html').getAttribute('lang')==='zh-CN','Chinese browser selects Chinese');
 assert(await z.getByLabel('主题',{exact:true}).inputValue()==='auto','New browser retains Auto default');
 const peer=await context.newPage();await peer.goto('http://127.0.0.1:13082/dios/#workbench');await peer.getByLabel('Language',{exact:true}).waitFor();
 await page.goto('http://127.0.0.1:13082/dios/#console/tasks');await page.getByLabel('Language',{exact:true}).selectOption('zh');await peer.getByLabel('语言',{exact:true}).waitFor();checks.push('Preference sync across tabs');
 assert(errors.length===0,'No browser errors');
 await fs.writeFile(dir+'/preferences-browser.json',JSON.stringify({checks,errors},null,2));console.log(JSON.stringify({passed:checks.length,errors}));
}finally{await browser.close();}
