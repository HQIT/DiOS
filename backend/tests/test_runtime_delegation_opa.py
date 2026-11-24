"""Native OPA delegation policy at the actual Runtime tool gate; isolated data."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
import httpx
from app.runtime.database import RuntimeErrorCode
import test_runtime_opa_integration as opa_fixtures


class DelegationOpaTests(opa_fixtures.OpaIntegrationTests):
    decision_path='/v1/data/dios/delegated_tools/allow'

    def write_bundle(self, revision):
        super().write_bundle(revision)
        source=Path(__file__).resolve().parents[2]/'examples/policies/opa-delegation'
        (self.bundle/'delegation.rego').write_text((source/'delegation.rego').read_text())
        (self.bundle/'delegated_tools.rego').write_text((source/'tools.rego').read_text())

    async def asyncSetUp(self):
        await super().asyncSetUp()
        chain=['root','first','second','third'];links={}
        for i, id in enumerate(chain):
            links[id]=dict(id=id,parent_ref=chain[i-1] if i else None,subject_ref='operator:test',
                actor_ref='agent:'+str(i) if i<3 else 'agent:test',scope=self.scope,audience='dios-runtime',
                permissions=[self.permission],depth=i,authorization_epoch=1,state='active',
                expires_at=(datetime.now(timezone.utc)+timedelta(minutes=30-i)).isoformat())
        # Identity comes from the trusted work-policy fixture, not test request fields.
        policy=json.loads(self.policy.path.read_text())
        for link in links.values():link['subject_ref']=policy['subject']
        links['third']['actor_ref']=policy['actor']
        self.data={'links':links,'work_bindings':{self.run['input']['work_authorization_ref']:
            dict(scope=self.scope,subject_ref=policy['subject'],actor_ref=policy['actor'],run_id=self.run['id'],
                chain=chain,epochs={id:1 for id in chain})}}
        await self.publish()

    async def publish(self):
        async with httpx.AsyncClient(trust_env=False,timeout=3) as client:
            response=await client.put(self.url+'/v1/data/dios_delegation',json=self.data)
            response.raise_for_status()

    async def denied(self, key):
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_DENIED'):
            await self.request(key)
        self.assertEqual(self.calls,[])

    async def test_each_ancestor_revocation_denies_and_unrelated_change_preserves(self):
        original=copy.deepcopy(self.data)
        for i,id in enumerate(original['links']):
            self.data=copy.deepcopy(original);self.data['links'][id]['state']='revoked';await self.publish()
            await self.denied('revoke-'+str(i))
        self.data=copy.deepcopy(original);self.data['links']['unrelated']=dict(original['links']['root'],state='revoked')
        await self.publish()
        item=await self.approve(await self.request('valid'))
        self.assertEqual((await self.gate.dispatch(self.run['id'],item['id'],item['version']))['state'],'succeeded')

    async def test_permission_tuple_expansion_and_cross_product_are_denied(self):
        original=copy.deepcopy(self.data)
        for i,field in enumerate(self.permission):
            self.data=copy.deepcopy(original)
            changed=dict(self.permission);changed[field]='expanded'
            self.data['links']['second']['permissions'].append(changed)
            await self.publish();await self.denied('expanded-'+str(i))
        self.data=copy.deepcopy(original)
        self.data['links']['root']['permissions']=[dict(self.permission,action='different'),dict(self.permission,resource_id='different')]
        await self.publish();await self.denied('cross-product')

    async def test_expiry_extension_and_expired_ancestor_are_denied(self):
        original=copy.deepcopy(self.data)
        self.data['links']['second']['expires_at']=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
        await self.publish();await self.denied('extended')
        self.data=copy.deepcopy(original)
        # All deadlines preserve parent/child ordering; only current expiry denies.
        for i,link in enumerate(self.data['links'].values()):
            link['expires_at']=(datetime(2000,1,1,tzinfo=timezone.utc)-timedelta(minutes=i)).isoformat()
        await self.publish();await self.denied('expired')

    async def test_depth_cycles_missing_links_and_audience_are_denied(self):
        original=copy.deepcopy(self.data);ref=self.run['input']['work_authorization_ref']
        mutations=[lambda d:d['work_bindings'][ref]['chain'].append('third'),
            lambda d:d['links']['second'].update(parent_ref='third'),
            lambda d:d['links'].pop('first'),lambda d:d['links']['third'].update(depth=4),
            lambda d:d['links']['first'].update(audience='other-service'),
            lambda d:d['links']['second'].update(scope='other-scope'),
            lambda d:d['links']['second'].update(subject_ref='other-subject'),
            lambda d:d['links']['third'].update(actor_ref='other-actor'),
            lambda d:d['links']['first'].update(authorization_epoch=1.5),
            lambda d:d['links']['first'].update(expires_at='invalid-time'),
            lambda d:d['links']['third']['permissions'].append(copy.deepcopy(self.permission)),
            lambda d:d['links']['third']['permissions'][0].update(unknown_constraint='allow')]
        for i,mutate in enumerate(mutations):
            self.data=copy.deepcopy(original);mutate(self.data);await self.publish();await self.denied('invalid-'+str(i))
        self.data=copy.deepcopy(original);leaf=copy.deepcopy(self.data['links']['third'])
        self.data['links']['fourth']=dict(leaf,id='fourth',parent_ref='third',depth=4)
        self.data['work_bindings'][ref]['chain'].append('fourth');self.data['work_bindings'][ref]['epochs']['fourth']=1
        await self.publish();await self.denied('fourth-level')

    async def test_root_and_one_to_three_levels_allow_real_call_with_narrowing(self):
        original=copy.deepcopy(self.data);ref=self.run['input']['work_authorization_ref']
        for depth in range(4):
            self.data=copy.deepcopy(original)
            receipt=self.data['work_bindings'][ref];receipt['chain']=receipt['chain'][:depth+1]
            receipt['epochs']={id:1 for id in receipt['chain']}
            self.data['links'][receipt['chain'][-1]]['actor_ref']=receipt['actor_ref']
            for i,id in enumerate(receipt['chain']):
                self.data['links'][id]['permissions']+=[dict(self.permission,resource_id='extra-'+str(j)) for j in range(i,depth)]
            await self.publish()
            item=await self.approve(await self.request('depth-'+str(depth)))
            self.assertEqual((await self.gate.dispatch(self.run['id'],item['id'],item['version']))['state'],'succeeded')
        self.assertEqual(len(self.calls),4)

    async def test_epoch_change_cannot_resurrect_old_work_after_restore(self):
        item=await self.approve(await self.request())
        self.data['links']['first'].update(state='revoked',authorization_epoch=2);await self.publish()
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_DENIED'):
            await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.data['links']['first']['state']='active';await self.publish()
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_DENIED'):
            await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.calls,[])

    async def test_missing_or_wrong_work_binding_cannot_borrow_authority(self):
        original=copy.deepcopy(self.data);ref=self.run['input']['work_authorization_ref']
        for i,field in enumerate(('run_id','actor_ref','subject_ref','scope')):
            self.data=copy.deepcopy(original);self.data['work_bindings'][ref][field]='unrelated'
            await self.publish();await self.denied('binding-'+str(i))
        self.data=copy.deepcopy(original);self.data['work_bindings']={};await self.publish();await self.denied('missing')

    async def test_upstream_revocation_after_mcp_initialization_prevents_call(self):
        from contextlib import asynccontextmanager
        item=await self.approve(await self.request());original=self.gate.transport;owner=self
        class Transport:
            @asynccontextmanager
            async def open(inner,*args):
                async with original.open(*args) as channel:
                    owner.data['links']['second']['state']='revoked';await owner.publish()
                    yield channel
        self.gate.transport=Transport()
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_DENIED'):
            await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
