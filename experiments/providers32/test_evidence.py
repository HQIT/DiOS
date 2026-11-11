"""Independent verifier rejects forged histories even after hashes are recomputed."""
import copy,unittest
from .model import action,initial,transition,verify,hashed

def history(requests):
    state=initial();entries=[]
    for q in requests:
        state,entry,result=transition(state,q,None)
        assert result['status']=='committed';entries.append(entry)
    return state,entries

def rehash(state,entries):
    previous='0'*64
    for e in entries:
        e['previous_hash']=previous;e['request_digest']=hashed(e['request'])
        e['hash']=hashed({k:v for k,v in e.items() if k!='hash'});previous=e['hash']
    state['head']=previous

class EvidenceTests(unittest.TestCase):
    def forged(self,change):
        state,entries=history([action('r','a'),action('r','b')])
        change(entries);rehash(state,entries)
        with self.assertRaises(AssertionError):verify(state,entries)
    def test_valid_chain(self):
        state,entries=history([action('r','a'),{'run_id':'r','id':'epoch','kind':'epoch'},action('r','b',epoch=2)])
        verify(state,entries)
    def test_rehashed_scope(self):self.forged(lambda e:e[1]['request'].update(resource='snapshot:other'))
    def test_rehashed_owner_epoch(self):self.forged(lambda e:e[1]['request'].update(epoch=9))
    def test_rehashed_authorization_epoch(self):self.forged(lambda e:e[1]['request'].update(authorization_epoch=9))
    def test_rehashed_double_approval(self):
        self.forged(lambda e:[x['request'].update(approval=True) for x in e])
    def test_rehashed_unknown_claimed_success(self):self.forged(lambda e:e[1].update(outcome='succeeded'))
    def test_truncated_chain(self):
        state,entries=history([action('r','a'),action('r','b')])
        with self.assertRaises(AssertionError):verify(state,entries[:1])

if __name__=='__main__':unittest.main()
