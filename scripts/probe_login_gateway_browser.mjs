import {chromium} from '../node_modules/playwright/index.mjs';
import {createInterface} from 'node:readline';
const browser=await chromium.launch({headless:true});const sessions=new Map();let base,issuer,password,savedCookies,lastCallback;
async function request(context,path='/who',options={}){const r=await context.request.fetch(base+path,{maxRedirects:0,...options});let data;try{data=await r.json()}catch{}return {status:r.status(),data};}
try{for await(const line of createInterface({input:process.stdin,crlfDelay:Infinity})){
 try{
  const m=JSON.parse(line);let result;
  if(m.action==='init'){({base,issuer,password}=m);result={ready:true};}
  else if(m.action==='login'){
   const context=await browser.newContext();const page=await context.newPage();let authorize;
   page.on('request',request=>{if(request.url().startsWith(base+'/oauth2/callback?'))lastCallback=request.url()});
   await page.route(issuer+'/login-actions/authenticate?**',async route=>{
    const response=await route.fetch({maxRedirects:0});const headers=response.headers();
    if(headers.location?.startsWith(base+'/oauth2/callback?')){
     lastCallback=headers.location;
     if(m.bad==='state'){const u=new URL(headers.location);u.searchParams.set('state','incorrect-state');headers.location=u.toString();}
    }
    await route.fulfill({response,headers});
   });
   const start=await context.request.get(base+'/oauth2/start?rd=%2Fwho',{maxRedirects:0});const u=new URL(start.headers().location);
   if(u.origin!==new URL(issuer).origin)throw Error('Unexpected authorization origin');
   authorize={s256:u.searchParams.get('code_challenge_method')==='S256',challenge:!!u.searchParams.get('code_challenge'),nonce:!!u.searchParams.get('nonce'),state:!!u.searchParams.get('state')};
   if(m.bad==='nonce')u.searchParams.set('nonce','incorrect-nonce');if(m.bad==='pkce')u.searchParams.set('code_challenge','A'.repeat(43));
   await page.goto(u.toString());await page.locator('#username').fill(m.username);await page.locator('#password').fill(password);
   await page.locator('#kc-login').click();await page.waitForURL(url=>url.origin===base);
   result=await request(context);result.authorize=authorize;
   result.cookies=(await context.cookies()).filter(c=>c.name.startsWith('_oauth2_proxy')).map(c=>({name:c.name,httpOnly:c.httpOnly,sameSite:c.sameSite,secure:c.secure,length:c.value.length}));
   result.storage=await page.evaluate(()=>({local:Object.keys(localStorage),session:Object.keys(sessionStorage)}));
   sessions.set(m.name,{context,page});
  }else{
   const {context,page}=sessions.get(m.name);
   if(m.action==='get')result=await request(context,m.path, m.options);
   if(m.action==='parallel')result=await Promise.all(Array.from({length:6},()=>request(context)));
   if(m.action==='replay'){const response=await context.request.get(lastCallback,{maxRedirects:0});result={status:response.status()};}
   if(m.action==='tamper'){
    const cookies=await context.cookies();const changed=cookies.map(c=>c.name==='_oauth2_proxy'?{...c,value:c.value.slice(0,-1)+(c.value.endsWith('a')?'b':'a')}:c);
    await context.clearCookies();await context.addCookies(changed);result=await request(context);await context.clearCookies();await context.addCookies(cookies);
   }
   if(m.action==='logout'){
    savedCookies=await context.cookies();const response=await context.request.get(base+'/oauth2/sign_out',{maxRedirects:0});result={status:response.status(),after:await request(context)};
   }
   if(m.action==='replay-cookie'){await context.clearCookies();await context.addCookies(savedCookies);result=await request(context);}
   if(m.action==='sso-login'){await page.goto(base+'/oauth2/start?rd=%2Fwho');await page.waitForURL(url=>url.origin===base);result=await request(context);}
  }
  process.stdout.write(JSON.stringify({ok:true,result})+'\n');
 }catch(e){process.stdout.write(JSON.stringify({ok:false,error:String(e.message).split('\n')[0].replace(/https?:\/\/\S+/g,'[url]')})+'\n');}
}}finally{await browser.close();}
