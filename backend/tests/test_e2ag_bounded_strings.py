import copy
import unittest
from app.services.e2ag_arguments import valid_constraints, check_arguments
from app.services.e2ag import verify_audit_chain
from app.models.tables import E2AGToolGrant
import test_e2ag_argument_constraints as previous
import test_e2ag_tool_gateway as fixture


POLICY = {'version': 2, 'tools': {'ledger.record_push': {
    'resource': {'type': 'string', 'enum': ['report/1']},
    'report': {'type': 'string', 'minLength': 1, 'maxLength': 4}}}}


class BoundedStringTests(unittest.TestCase):
    def check(self, value, policy=POLICY):
        return check_arguments('ledger.record_push', {'resource': 'report/1', 'report': value}, policy)

    def test_boundaries_and_unicode(self):
        for value in ('a', 'abcd', '中文', '\U0001f642', 'e\u0301'):
            with self.subTest(value=value): self.assertTrue(self.check(value)[0])
        for value in ('', 'abcde', '中' * 5):
            with self.subTest(value=value): self.assertEqual(self.check(value)[1], 'MCP_ARGUMENT_LENGTH_DENIED')

    def test_not_content_quality_filter(self):
        self.assertTrue(self.check('错答')[0])

    def test_type_no_coercion(self):
        for value in (None, True, 2, 2.0, [], {}):
            self.assertEqual(self.check(value)[1], 'MCP_ARGUMENT_TYPE_DENIED')

    def test_surrogates_fail_closed(self):
        for value in ('\ud800', '\udfff', 'a\ud800'):
            self.assertEqual(self.check(value)[1], 'MCP_ARGUMENT_UNICODE_DENIED')

    def test_code_points_not_graphemes_or_utf8_bytes(self):
        p = copy.deepcopy(POLICY); p['tools']['ledger.record_push']['report']['maxLength'] = 1
        self.assertTrue(self.check('\U0001f642', p)[0])
        self.assertFalse(self.check('e\u0301', p)[0])

    def test_empty_permitted_only_when_configured(self):
        p = copy.deepcopy(POLICY); p['tools']['ledger.record_push']['report']['minLength'] = 0
        self.assertTrue(self.check('', p)[0])

    def test_v1_unchanged(self):
        p = copy.deepcopy(POLICY); p['version'] = 1
        self.assertFalse(valid_constraints(p))
        self.assertTrue(valid_constraints(previous.POLICY))
        self.assertTrue(check_arguments('ledger.record_push', previous.ARGS, previous.POLICY)[0])

    def test_bad_schema(self):
        for low, high in ((-1, 4), (3, 2), (True, 4), (0, 1.0), (0, 262145)):
            p = copy.deepcopy(POLICY); p['tools']['ledger.record_push']['report'].update(minLength=low, maxLength=high)
            self.assertFalse(valid_constraints(p))
        p = copy.deepcopy(POLICY); p['tools']['ledger.record_push']['report']['enum'] = ['x']
        self.assertFalse(valid_constraints(p))

    def test_resource_and_closed_fields_remain_enforced(self):
        for args in ({'resource': 'report/2', 'report': 'a'}, {'resource': 'report/1'},
                     {'resource': 'report/1', 'report': 'a', 'extra': 'x'}):
            self.assertFalse(check_arguments('ledger.record_push', args, POLICY)[0])


class BoundedStringIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.E2AGToolGatewayTests.asyncSetUp
    asyncTearDown = fixture.E2AGToolGatewayTests.asyncTearDown
    _fixtures = fixture.E2AGToolGatewayTests._fixtures
    configured = previous.ArgumentConstraintIntegrationTests.configured
    invoke = previous.ArgumentConstraintIntegrationTests.invoke

    async def test_snapshot_denial_then_actual_unicode_forwarding(self):
        async with self.sessions() as db:
            agent, _, event, _, grant, token = await self.configured(db, POLICY)
            widened = copy.deepcopy(POLICY); widened['tools']['ledger.record_push']['report']['maxLength'] = 100
            agent.capabilities = {'governance': {'allowed_tools': ['ledger.*'], 'argument_constraints': widened}}
            await db.commit()
            async with self.sessions() as reader:
                saved = await reader.get(E2AGToolGrant, grant.id)
                self.assertEqual(saved.argument_constraints, POLICY)
            response = await self.invoke(db, grant, token, {'resource': 'report/1', 'report': 'abcde'})
            self.assertEqual(response.status_code, 403)
            self.assertFalse(fixture.FakeAsyncClient.calls)
            good = {'resource': 'report/1', 'report': '中文\U0001f642'}
            response = await self.invoke(db, grant, token, good)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(fixture.FakeAsyncClient.calls[-1]['json']['params']['arguments'], good)
            await db.refresh(event)
            self.assertTrue(verify_audit_chain(event.audit_chain))
            self.assertEqual([e['outcome'] for e in event.audit_chain if e['stage'] == 'tool_call'], ['deny', 'allow'])
