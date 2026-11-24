import {chromium} from '../node_modules/playwright/index.mjs';
import {createServer} from 'node:http';
let input='';for await(const chunk of process.stdin)input+=chunk;
const {url,username,password,redirect}=JSON.parse(input);
const target=new URL(redirect);if(target.hostname!=='127.0.0.1')throw Error('Loopback callback required');
let resolveCallback;const callback=new Promise(resolve=>{resolveCallback=resolve});
const server=createServer((req,res)=>{if(req.url.startsWith(target.pathname+'?'))resolveCallback(target.origin+req.url);res.writeHead(200,{'Content-Type':'text/html'});res.end('Authentication callback received.');});
await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(Number(target.port),'127.0.0.1',resolve)});
const browser=await chromium.launch({headless:true});let timer;
try{
 const page=await browser.newPage();await page.goto(url);
 await page.locator('#username').fill(username);await page.locator('#password').fill(password);await page.locator('#kc-login').click();
 const result=await Promise.race([callback,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('OIDC callback timeout')),10000)})]);
 process.stdout.write(JSON.stringify({callback:result}));
}finally{clearTimeout(timer);await browser.close();await new Promise(resolve=>server.close(resolve))}
