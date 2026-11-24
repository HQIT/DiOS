"""Isolated official Keycloak/Authlib protocol qualification; no production data."""
import argparse,json,os,secrets,socket,subprocess,tempfile,time
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import httpx
from authlib.integrations.httpx_client import OAuth2Client
from authlib.oauth2.rfc6749.errors import MismatchingStateException
from authlib.integrations.base_client.errors import OAuthError as OAuth2Error
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet
from joserfc.jws import JWSRegistry
from joserfc.errors import InvalidClaimError

IMAGE='quay.io/keycloak/keycloak:26.8.2@sha256:7e66a96e4739239070aeaf72e4c5decf0f7f682cfc725b9f7ea2b336eaba38b3'
ROOT=Path(__file__).resolve().parents[1]

def run(output,node):
 checks=[];observations={};cid=None
 def check(name,condition):
  if not condition:raise AssertionError(name)
  checks.append(name);print(name,flush=True)
 def docker(*args):return subprocess.check_output(['docker',*args],text=True,stderr=subprocess.PIPE).strip()
 with tempfile.TemporaryDirectory(prefix='dios-identity-') as tmp:
  directory=Path(tmp);directory.chmod(0o755)
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  base=f'http://127.0.0.1:{port}';issuer=base+'/realms/dios-qualification';redirect='http://127.0.0.1:17654/callback'
  password=secrets.token_urlsafe(24);api_secret=secrets.token_urlsafe(32);tool_secret=secrets.token_urlsafe(32);admin_password=secrets.token_urlsafe(32)
  def audience(name):return {'name':'aud-'+name,'protocol':'openid-connect','protocolMapper':'oidc-audience-mapper','config':{'included.client.audience':name,'access.token.claim':'true','id.token.claim':'false','introspection.token.claim':'true'}}
  realm={'realm':'dios-qualification','enabled':True,'sslRequired':'none','accessTokenLifespan':300,'revokeRefreshToken':True,'refreshTokenMaxReuse':0,
   'users':[{'username':n,'enabled':True,'email':n+'@example.invalid','emailVerified':True,'firstName':n,'lastName':'Qualification','credentials':[{'type':'password','value':password,'temporary':False}]} for n in ('alice','bob')],
   'clients':[
    {'clientId':'portal','publicClient':True,'standardFlowEnabled':True,'directAccessGrantsEnabled':False,'redirectUris':[redirect],'attributes':{'pkce.code.challenge.method':'S256'},'protocolMappers':[audience('dios-api')]},
    {'clientId':'dios-api','secret':api_secret,'publicClient':False,'standardFlowEnabled':False,'serviceAccountsEnabled':True,'attributes':{'standard.token.exchange.enabled':'true'},'protocolMappers':[audience('tool-service')]},
    {'clientId':'tool-service','secret':tool_secret,'publicClient':False,'standardFlowEnabled':False,'serviceAccountsEnabled':True},
    {'clientId':'stranger','secret':secrets.token_urlsafe(32),'publicClient':False,'standardFlowEnabled':False}]}
  imp=directory/'import';imp.mkdir(mode=0o755);(imp/'realm.json').write_text(json.dumps(realm));(imp/'realm.json').chmod(0o644)
  env=directory/'env';env.write_text('KC_BOOTSTRAP_ADMIN_USERNAME=qualification-admin\nKC_BOOTSTRAP_ADMIN_PASSWORD='+admin_password+'\n');env.chmod(0o600)
  try:
   cid=docker('run','-d','--label','dios.purpose=identity-qualification','--memory=1g','--cpus=2','-p',f'127.0.0.1:{port}:8080','--env-file',str(env),'-v',str(imp)+':/opt/keycloak/data/import:ro',IMAGE,'start-dev','--import-realm','--hostname',base)
   http=httpx.Client(timeout=10,trust_env=False,follow_redirects=False)
   def ready():
    for _ in range(180):
     try:
      response=http.get(issuer+'/.well-known/openid-configuration')
      if response.status_code==200:return response.json()
     except httpx.HTTPError:pass
     time.sleep(.5)
    raise AssertionError('provider-ready-timeout')
   metadata=ready();check('discovery-issuer-and-introspection',metadata['issuer']==issuer and metadata['introspection_endpoint'].startswith(issuer+'/'))
   endpoint=metadata['token_endpoint'];introspection=metadata['introspection_endpoint'];revocation=metadata['revocation_endpoint']
   def client(name='portal',secret=None,**kw):return OAuth2Client(name,secret,token_endpoint_auth_method='client_secret_basic' if secret else 'none',timeout=10,trust_env=False,follow_redirects=False,**kw)
   def introspect(token,name='dios-api',secret=None):
    with client(name,secret or api_secret) as c:
     response=c.introspect_token(introspection,token=token,token_type_hint='access_token');response.raise_for_status();return response.json()
   def login(username='alice',bad_verifier=False,bad_state=False,bad_nonce=False):
    verifier=secrets.token_urlsafe(48);nonce=secrets.token_urlsafe(24)
    with client(redirect_uri=redirect,scope='openid',code_challenge_method='S256') as c:
     url,state=c.create_authorization_url(metadata['authorization_endpoint'],code_verifier=verifier,nonce=nonce)
     result=subprocess.run([node,str(ROOT/'scripts/probe_identity_login.mjs')],input=json.dumps({'url':url,'username':username,'password':password,'redirect':redirect}),capture_output=True,text=True,timeout=40)
     if result.returncode:
      import re
      raise AssertionError('browser-login-failed: '+re.sub(r'http[^\s]+','[url]',result.stderr)[-1800:])
     callback=json.loads(result.stdout)['callback']
     if bad_state:callback=callback.replace('state='+state,'state=wrong')
     token=c.fetch_token(endpoint,authorization_response=callback,state=state,code_verifier='wrong-verifier-'+verifier if bad_verifier else verifier)
     decoded=jwt.decode(token['id_token'],key=KeySet.import_key_set(http.get(metadata['jwks_uri']).json()),registry=JWSRegistry(algorithms=['RS256']))
     claims=CodeIDToken(decoded.claims,decoded.header,{'iss':{'essential':True,'value':issuer}},{'nonce':'wrong' if bad_nonce else nonce,'client_id':'portal','access_token':token['access_token']});claims.validate(leeway=0)
     return dict(token),parse_qs(urlsplit(callback).query)['code'][0],verifier
   token,code,verifier=login();active=introspect(token['access_token'])
   check('browser-authorization-code-S256',active.get('active') is True and active.get('sub') and active.get('iss')==issuer)
   check('audience-and-client-separation','dios-api' in active.get('aud',[]) and active.get('client_id')=='portal')
   try:login(bad_state=True)
   except MismatchingStateException:check('callback-state-mismatch-rejected',True)
   else:raise AssertionError('callback-state-mismatch-accepted')
   try:login(bad_nonce=True)
   except InvalidClaimError:check('ID-token-nonce-mismatch-rejected',True)
   else:raise AssertionError('ID-token-nonce-mismatch-accepted')
   try:login(bad_verifier=True)
   except OAuth2Error:check('PKCE-wrong-verifier-rejected',True)
   else:raise AssertionError('PKCE-wrong-verifier-accepted')
   with client(redirect_uri=redirect) as c:
    try:c.fetch_token(endpoint,code=code,code_verifier=verifier)
    except OAuth2Error:check('authorization-code-replay-rejected',True)
    else:raise AssertionError('code-replay-accepted')
   check('unknown-token-inactive',introspect(secrets.token_urlsafe(48)).get('active') is False)
   token,_,_=login();check('audience-positive-control',introspect(token['access_token']).get('active') is True)
   wrong=http.post(introspection,auth=('dios-api','wrong-secret'),data={'token':token['access_token']})
   check('introspection-client-authentication',wrong.status_code in (400,401,403))
   alien=introspect(token['access_token'],'tool-service',tool_secret)
   check('wrong-resource-audience-inactive',alien.get('active') is False)
   bob,_,_=login('bob');check('distinct-user-subjects',introspect(bob['access_token'])['sub']!=active['sub'])
   # Rotate a fresh refresh token, then prove the consumed token cannot be replayed.
   rotating,_,_=login()
   with client() as c:
    refreshed=dict(c.refresh_token(endpoint,refresh_token=rotating['refresh_token']))
    check('refresh-preserves-subject',introspect(refreshed['access_token'])['sub']==active['sub'])
    check('refresh-token-rotated',refreshed['refresh_token']!=rotating['refresh_token'])
    try:c.refresh_token(endpoint,refresh_token=rotating['refresh_token'])
    except OAuth2Error:check('refresh-token-replay-rejected',True)
    else:raise AssertionError('refresh-replay-accepted')
   # Exchange and revoke a separate source token. Record actual revocation semantics.
   source,_,_=login()
   with client('dios-api',api_secret) as c:
    exchanged=dict(c.fetch_token(endpoint,grant_type='urn:ietf:params:oauth:grant-type:token-exchange',subject_token=source['access_token'],subject_token_type='urn:ietf:params:oauth:token-type:access_token',audience='tool-service'))
   child=introspect(exchanged['access_token'],'tool-service',tool_secret);parent=introspect(source['access_token'])
   check('exchange-preserves-subject',child.get('active') is True and child['sub']==parent['sub'])
   check('exchange-narrows-audience',child.get('aud')=='tool-service' or child.get('aud')==['tool-service'])
   observations['exchange_expiry_within_source']=child['exp']<=parent['exp']
   observations['exchange_actor_claim_present']='act' in child
   with client() as c:check('source-revocation-endpoint',c.revoke_token(revocation,token=source['access_token'],token_type_hint='access_token').status_code==200)
   check('source-access-token-inactive',introspect(source['access_token']).get('active') is False)
   observations['exchanged_access_active_after_source_revocation']=introspect(exchanged['access_token'],'tool-service',tool_secret).get('active')
   check('unrelated-user-remains-active',introspect(bob['access_token']).get('active') is True)
   # Explicit user-session logout must invalidate its active token on a fresh query.
   logout,_,_=login()
   result=http.post(metadata['end_session_endpoint'],data={'client_id':'portal','refresh_token':logout['refresh_token']})
   check('session-logout-accepted',result.status_code in (200,204))
   check('session-logout-introspection-inactive',introspect(logout['access_token']).get('active') is False)
   # Service principal differs from user and uses the same resource audience check.
   with client('dios-api',api_secret) as c:service=dict(c.fetch_token(endpoint,grant_type='client_credentials'))
   serviceinfo=introspect(service['access_token'],'tool-service',tool_secret)
   check('service-identity-distinct',serviceinfo.get('active') is True and serviceinfo['sub']!=active['sub'])
   # Keep the container filesystem and restart the isolated IdP; check retained sessions.
   docker('restart',cid);ready();check('provider-restart-retains-session',introspect(bob['access_token']).get('active') is True)
   docker('stop',cid)
   try:introspect(bob['access_token'])
   except httpx.HTTPError:check('provider-offline-is-observation-failure',True)
   else:raise AssertionError('offline-provider-returned-active')
   proof={'date':'2026-11-24','image':IMAGE,'library':{'Authlib':'1.9.10','httpx':'0.28.1'},'checks':checks,'observations':observations,'limits':['isolated-loopback-development-IdP','embedded-storage-restart-only','no-DiOS-runtime-SSO-integration','no-physical-multihost-or-TLS-qualification','standard-token-exchange-not-delegation-qualification']}
   output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
   print(json.dumps({'passed':len(checks),'observations':observations}),flush=True)
  finally:
   if cid:docker('rm','-f',cid)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--node',default='node');args=parser.parse_args();run(args.output,args.node)
