"""Offline tests using fakes only; never reaches Panorama."""
import importlib.util
import json
import sys
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

try:
    import requests  # noqa: F401
except ImportError:
    # Offline tests never touch the network; stub the imports so the
    # suite is runnable anywhere without extra packages.
    if 'requests' not in sys.modules:
        _req = types.ModuleType('requests')

        class _NoNetSession:
            def __getattr__(self, _name):
                raise RuntimeError('network use in offline test')
        _req.Session = _NoNetSession
        sys.modules['requests'] = _req
    else:
        _req = sys.modules['requests']
        if not hasattr(_req, 'Session'):
            class _NoNetSession:
                def __getattr__(self, _n):
                    raise RuntimeError('network use in offline test')
            _req.Session = _NoNetSession
    _u3 = types.ModuleType('urllib3')
    _u3.disable_warnings = lambda *a, **k: None
    sys.modules.setdefault('urllib3', _u3)

spec=importlib.util.spec_from_file_location('panorama_cli', Path(__file__).resolve().with_name('panorama_cli.py'))
p=importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class FakeClient(p.PanoramaClient):
    def __init__(self):
        super().__init__('example.invalid', 'DELIBERATELY-FAKE')
        self.rule = {
            '@name':'My Rule With Spaces',
            '@location':'device-group',
            '@device-group':'DG',
            'from': {'member':['untrust']},
            'to': {'member':['trust']},
            'source': {'member':['original-host','concurrently-added-host']},
            'destination': {'member':['dest-a']},
            'application': {'member':['any']},
            'action':'allow', 'description':'Preserve this setting',
        }
        self.calls=[]
        self.fail_rest=None
        self.write_xml=False
    def rest_base(self):
        return '/restapi/v11.0'
    def _rest(self, method, path, params=None, body=None):
        self.calls.append((method,path,deepcopy(params),deepcopy(body)))
        if self.fail_rest:
            raise RuntimeError(self.fail_rest)
        if method=='GET':
            return {'@status':'success','result':{'entry':[deepcopy(self.rule)]}}
        if method=='PUT':
            assert path=='/restapi/v11.0/Policies/SecurityPostRules'
            assert params=={'location':'device-group','device-group':'DG','name':'My Rule With Spaces'}
            self.rule=deepcopy(body['entry'][0])
            return {'@status':'success','result':{}}
        raise AssertionError('no POST allowed')
    def config_rule_field_read(self,path,field):
        self.calls.append(('XML GET',path))
        if not self.write_xml:
            raise RuntimeError('XML unauth code 16')
        return ['original-host']
    def config_rule_field_edit(self,path,field,members):
        self.calls.append(('XML EDIT',path,field,deepcopy(members)))
        return {}

class RuleFixTests(unittest.TestCase):
    def record(self):
        return {'location':'DG','_rb':'post-rulebase/security',
                'rule':'My Rule With Spaces','_rule':{'source':{'member':'original-host'}}}
    def test_post_endpoint(self):
        self.assertEqual(p.PanoramaClient._policy_resource('post-rulebase/security'),'/Policies/SecurityPostRules')
        self.assertEqual(p.PanoramaClient._policy_resource('pre-rulebase/security'),'/Policies/SecurityPreRules')
    def test_xml_path(self):
        path=p.rule_field_paths('DG','post-rulebase/security','My Rule With Spaces','source')[0]
        self.assertEqual(path,"/config/devices/entry[@name='localhost.localdomain']/device-group/entry[@name='DG']/post-rulebase/security/rules/entry[@name='My Rule With Spaces']/source")
    def test_preserves_old_and_new_candidate_members(self):
        c=FakeClient()
        success,error=p._write_rule_field(c,self.record(),'source',{'member':['original-host','new-a','new-b']})
        self.assertTrue(success,error)
        self.assertEqual(c.rule['source']['member'], ['original-host','concurrently-added-host','new-a','new-b'])
        self.assertEqual(c.rule['description'],'Preserve this setting')
        self.assertEqual([i[0] for i in c.calls], ['GET','PUT'])
    def test_duplicate_idempotent(self):
        c=FakeClient()
        success,error=p._write_rule_field(c,self.record(),'source',{'member':['original-host','concurrently-added-host']})
        self.assertTrue(success,error)
        self.assertFalse(any(i[0]=='PUT' for i in c.calls))
    def test_stale_candidate_refuses_write(self):
        c=FakeClient(); c.rule['source']={'member':['different-original']}
        success,error=p._write_rule_field(c,self.record(),'source',{'member':['original-host','new-a']})
        self.assertFalse(success); self.assertIn('changed since search',error)
        self.assertFalse(any(i[0]=='PUT' for i in c.calls))
    def test_revert_refuses_concurrent_changes(self):
        c=FakeClient()
        success,error=p._write_rule_field(c,self.record(),'source',{'member':['original-host']},
                                         expected_current=['original-host','new-a'])
        self.assertFalse(success); self.assertIn('unsafe revert',error)
    def test_fallback_xml_path_and_members(self):
        c=FakeClient(); c.fail_rest='HTTP 501 route not found'; c.write_xml=True
        success,error=p._write_rule_field(c,self.record(),'source',{'member':['original-host','new-a']})
        self.assertTrue(success,error)
        edits=[i for i in c.calls if i[0]=='XML EDIT']
        self.assertEqual(len(edits),1)
        self.assertEqual(edits[0][-1], ['original-host','new-a'])
    def test_fallback_cli_is_non_destructive(self):
        rec=self.record()
        plan=[(rec,'source',['original-host'],{'member':['original-host']},
               {'member':['original-host','new-a','new-b']},['new-a','new-b'])]
        with patch.object(p,'safe_filename',return_value='/tmp/test.txt'):
            fname,output=p._rule_cli_script('query',plan,[('FAILED','no route')])
        self.assertIn('set device-group "DG" post-rulebase security rules '
                      '"My Rule With Spaces" source member "new-a"', output)
        self.assertNotIn('clear device-group', output)
        self.assertEqual(output.count('\nset device-group'), 2)  # per new object

    def test_indirect_group_only_is_not_edited(self):
        indirect = {**self.record(), 'rule': 'Via group',
                    '_rule': {'source': {'member': 'grp-hosts'}},
                    '_fields': ['source'], '_direct_fields': []}
        direct = {**self.record(), 'rule': 'Direct',
                  '_rule': {'source': {'member': 'original-host'}},
                  '_fields': ['source'], '_direct_fields': ['source']}
        with patch.object(p, '_prompt_add_names', return_value=['new-a']), \
             patch.object(p, '_write_rule_field', return_value=(True, '')) as writer, \
             patch.object(p, '_rule_edit_diff', return_value=('diff.txt', 'diff')), \
             patch.object(p, '_move_print_diff'), \
             patch.object(p, 'choose', return_value='leave pending'), \
             patch('builtins.input', return_value='yes'):
            p._add_to_rules(FakeClient(), {}, [indirect, direct], 'original-host')
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(writer.call_args.args[1]['rule'], 'Direct')

    def test_group_only_short_circuits_before_name_prompt(self):
        indirect = {**self.record(), '_fields': ['source'], '_direct_fields': [],
                    '_rule': {'source': {'member': 'grp-hosts'}}}
        with patch.object(p, '_prompt_add_names') as prompt, \
             patch.object(p, '_write_rule_field') as writer:
            p._add_to_rules(FakeClient(), {}, [indirect], 'original-host')
        prompt.assert_not_called()
        writer.assert_not_called()

    def test_direct_only_source_preserves_indirect_destination(self):
        rec = {**self.record(),
               '_fields': ['source', 'destination'],
               '_direct_fields': ['source'],
               '_rule': {'source': {'member': 'original-host'},
                         'destination': {'member': 'grp-hosts'}}}
        with patch.object(p, '_prompt_add_names', return_value=['new-a']), \
             patch.object(p, '_write_rule_field', return_value=(True, '')) as writer, \
             patch.object(p, '_rule_edit_diff', return_value=('diff.txt', 'diff')), \
             patch.object(p, '_move_print_diff'), \
             patch.object(p, 'choose', return_value='leave pending'), \
             patch('builtins.input', return_value='yes'):
            p._add_to_rules(FakeClient(), {}, [rec], 'original-host')
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(writer.call_args.args[2], 'source')

    def test_where_used_tracks_direct_and_indirect_separately(self):
        objects = {
            'original-host': {'name': 'original-host', 'type': 'address',
                              'location': '(shared)',
                              'fields': {'ip-netmask': '10.1.1.1'}},
            'grp-hosts': {'name': 'grp-hosts', 'type': 'address-group',
                          'location': '(shared)',
                          'fields': {'static': {'member': 'original-host'}}},
        }
        container = {'post-rulebase': {'security': {'rules': {'entry': [
            {'name': 'Only group', 'source': {'member': 'grp-hosts'}},
            {'name': 'Direct reference', 'destination': {'member': 'original-host'}},
            {'name': 'Both fields', 'source': {'member': 'original-host'},
             'destination': {'member': 'grp-hosts'}},
        ]}}}}
        with patch.object(p, 'find_device_group', return_value=container), \
             patch.object(p, '_add_to_rules') as editor, \
             patch('builtins.input', side_effect=['', '2']):
            p._where_used_once(FakeClient(), ['DG'], objects,
                               {'grp-hosts': {'original-host'}}, 'original-host')
        records = {r['rule']: r for r in editor.call_args.args[2]}
        self.assertEqual(records['Only group']['_fields'], ['source'])
        self.assertEqual(records['Only group']['_direct_fields'], [])
        self.assertEqual(records['Direct reference']['_direct_fields'], ['destination'])
        self.assertEqual(records['Both fields']['_direct_fields'], ['source'])
        self.assertEqual(records['Both fields']['_fields'], ['destination', 'source'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
