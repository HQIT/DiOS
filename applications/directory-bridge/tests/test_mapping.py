import copy
import json
from pathlib import Path
import tempfile
import unittest

from dios_directory.mapping import plan, load, MappingError, LIST, USER, GROUP


def listing(schema, rows):
    return {"schemas": [LIST], "totalResults": len(rows),
            "Resources": [dict(schemas=[schema], **row) for row in rows]}


def fixture():
    snapshot = {"version": 1, "source": "directory:test", "observed_at": 1000,
                "users": listing(USER, [{"id": "u1", "active": True}, {"id": "u2", "active": True}]),
                "groups": listing(GROUP, [{"id": "engineering", "members": [{"value": "u1"}]},
                                          {"id": "sales", "members": [{"value": "u2"}]}])}
    grant = {"review": {"actions": ["run.submit", "run.read"], "permissions": []}}
    mapping = {"version": 1, "source": "directory:test", "issuer": "trusted:test", "scope": "one",
               "subjects": {"u1": "p1", "u2": "p2"}, "roles": {"engineering": grant, "sales": grant}}
    current = [{"issuer": "trusted:test", "scope": "one", "principal_id": pid,
                "revision": 1, "enabled": True, "profiles": {}} for pid in ("p1", "p2")]
    return snapshot, mapping, current


class MappingTests(unittest.TestCase):
    def test_deterministic_union_and_noop_after_publish(self):
        s, m, c = fixture()
        s["groups"]["Resources"][1]["members"].append({"value": "u1"})
        first = plan(s, m, c, now=1001)
        self.assertEqual(len(first["patches"]), 2)
        self.assertEqual(first["patches"][0]["source_group_ids"], ["engineering", "sales"])
        for patch, current in zip(first["patches"], c):
            current.update(patch["changes"], revision=2)
        self.assertEqual(plan(s, m, c, now=1002)["patches"], [])
        self.assertEqual(first["valid_until"], 1300)

    def test_disabled_and_deleted_user_clear_profile_authority(self):
        for removed in (False, True):
            s, m, c = fixture()
            if removed:
                s["users"] = listing(USER, [{"id": "u2", "active": True}])
                s["groups"]["Resources"][0]["members"] = []
            else:
                s["users"]["Resources"][0]["active"] = False
            p = plan(s, m, c, now=1000)["patches"][0]
            self.assertEqual(p["principal_id"], "p1")
            self.assertEqual(p["changes"], {"enabled": False, "profiles": {}})

    def test_group_id_not_display_name_or_user_claim_controls_access(self):
        s, m, c = fixture()
        s["users"]["Resources"][0]["groups"] = [{"value": "sales"}]
        s["groups"]["Resources"][0].update(id="unknown", displayName="engineering")
        result = plan(s, m, c, now=1000)
        self.assertEqual(result["unchanged"], ["p1"])
        self.assertEqual([p["principal_id"] for p in result["patches"]], ["p2"])

    def test_partial_stale_wrong_source_or_future_snapshots_rejected(self):
        for mutate in (lambda s: s["users"].update(totalResults=20),
                       lambda s: s["groups"].update(startIndex=2),
                       lambda s: s.update(observed_at=0), lambda s: s.update(observed_at=1002),
                       lambda s: s.update(source="untrusted")):
            s, m, c = fixture(); mutate(s)
            with self.subTest(snapshot=s), self.assertRaises(MappingError):
                plan(s, m, c, now=1001)

    def test_duplicate_unknown_and_nested_members_rejected(self):
        for change in ("duplicate", "active", "unknown", "nested"):
            s, m, c = fixture()
            if change == "duplicate": s["users"]["Resources"][1]["id"] = "u1"
            if change == "active": s["users"]["Resources"][0].pop("active")
            if change == "unknown": s["groups"]["Resources"][0]["members"] = [{"value": "missing"}]
            if change == "nested": s["groups"]["Resources"][0]["members"] = [{"value": "u1", "type": "Group"}]
            with self.subTest(change=change), self.assertRaises(MappingError):
                plan(s, m, c, now=1001)

    def test_policy_namespace_credentials_and_duplicate_owner_rejected(self):
        for change in ("scope", "issuer", "secret", "duplicate", "unprovisioned"):
            s, m, c = fixture()
            if change in ("scope", "issuer"): c[0][change] = "other"
            if change == "secret": c[0]["tokens"] = ["secret"]
            if change == "duplicate": m["subjects"]["u2"] = "p1"
            if change == "unprovisioned": m["subjects"]["u1"] = "new"
            with self.subTest(change=change), self.assertRaises(MappingError):
                plan(s, m, c, now=1001)

    def test_invalid_grants_fail_without_partial_plan(self):
        for grant in ({"actions": ["admin"], "permissions": []},
                      {"actions": ["run.submit"], "permissions": []},
                      {"actions": ["run.read"], "permissions": [{"action": "execute"}]}):
            s, m, c = fixture(); m["roles"]["sales"] = {"review": grant}
            with self.assertRaises(MappingError): plan(s, m, c, now=1001)

    def test_duplicate_json_and_oversized_input_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "input.json"
            for raw in ('{"active":false,"active":true}', ' ' * (4 * 1024 * 1024 + 1)):
                path.write_text(raw)
                with self.assertRaises(MappingError): load(path)

    def test_role_removal_preserves_other_roles_and_input(self):
        s, m, c = fixture()
        m["roles"]["sales"] = {"observe": {"actions": ["run.read"], "permissions": []}}
        s["groups"]["Resources"][1]["members"].append({"value": "u1"})
        original = copy.deepcopy((s, m, c))
        both = plan(s, m, c, now=1000)["patches"][0]
        self.assertEqual(set(both["changes"]["profiles"]), {"review", "observe"})
        self.assertEqual((s, m, c), original)


if __name__ == "__main__": unittest.main()
