// Native SSO session supplies the issuer. A local driver provisions the trusted
// parent, checks actual Worker execution and signals when to log out.
import fs from 'node:fs/promises';
import {chromium} from '../node_modules/playwright/index.mjs';
const [fixturePath,output]=process.argv.slice(2);
const fixture=JSON.parse(await fs.readFile(fixturePath,'utf8'));
const base=new URL(fixture.base);
if(!['localhost','127.0.0.1'].includes(base.hostname))throw Error('Loopback fixture required');
const browser=await chromium.launch({headless:true});const checks=[];
async function wait(suffix){
  for(let i=0;i<240;i++){
    if(await fs.stat(output+suffix).catch(()=>null))return JSON.parse(await fs.readFile(output+suffix,'utf8'));
    await new Promise(r=>setTimeout(r,500));
  }
  throw Error('Driver deadline: '+suffix);
}
function check(name,value){if(!value)throw Error(name);checks.push(name);}
try{
  const context=await browser.newContext({locale:'zh-CN'});const page=await context.newPage();
  await page.goto(base+'dios/#console/tasks');await page.getByRole('link',{name:'企业登录',exact:true}).click();
  await page.locator('#username').fill(fixture.username);await page.locator('#password').fill(fixture.password);await page.locator('#kc-login').click();
  await page.getByLabel('工作目标',{exact:true}).waitFor();
  async function api(suffix,body){return await page.evaluate(async({suffix,body})=>{
    const response=await fetch('/dios/api/os/runtime'+suffix,{method:body?'POST':'GET',
      headers:body?{'Content-Type':'application/json','X-CSRF-Token':'1'}:{},body:body?JSON.stringify(body):undefined});
    return {status:response.status,value:await response.json()};
  },{suffix,body});}
  const identity=await api('/identity');check('native-sso-issuer',identity.status===200&&Boolean(identity.value.principal?.upstream?.session_id));
  await fs.writeFile(output+'.identity',JSON.stringify(identity.value),{mode:0o600});
  const request=await wait('.request');
  const child=await api('/delegations',request);check('authenticated-api-issuance',child.status===201);
  const replay=await api('/delegations',request);check('issuance-idempotency',replay.status===201&&replay.value.id===child.value.id);
  const detail=await api('/delegations/'+child.value.id);check('issuer-audit',detail.status===200&&detail.value.events.some(e=>e.details?.authenticated_issuer?.id===identity.value.principal.id));
  await fs.writeFile(output+'.issued',JSON.stringify(child.value),{mode:0o600});
  await wait('.logout');await page.getByRole('button',{name:'退出登录',exact:true}).click();
  await page.getByRole('link',{name:'企业登录',exact:true}).waitFor();
  check('native-issuer-signout',true);await fs.writeFile(output+'.logged-out','true');
  await fs.writeFile(output,JSON.stringify({checks},null,2));console.log(JSON.stringify({checks}));
}finally{await browser.close();}
