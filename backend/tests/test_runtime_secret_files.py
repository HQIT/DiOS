import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from app.runtime.database import RuntimeErrorCode
from app.runtime.openbao_secrets import OpenBaoSecrets
from app.runtime.secret_provider import FileSecretMounts,private_file,validate_worker_mounts


class SecretFileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'model-v1'
        self.raw='synthetic-中文\nsecret'.encode();self.path.write_bytes(self.raw);self.path.chmod(0o600)
        self.row=dict(state='active',version='v1',scope='test',profiles=['profile'],path=str(self.path),sha256=hashlib.sha256(self.raw).hexdigest())
        self.config={'version':1,'secrets':{'model':self.row}}
        self.catalog=self.root/'catalog.json';self.save()
        self.requests=[dict(ref='model',version='v1',target='/run/secrets/model')]
        self.provider=FileSecretMounts(self.catalog,'test')

    def save(self):self.catalog.write_text(json.dumps(self.config));self.catalog.chmod(0o600)
    def resolve(self):return self.provider.resolve('profile',self.requests)

    def managed(self):
        self.token=self.root/'agent-token';self.token.write_text('synthetic-service-token');self.token.chmod(0o600)
        self.config['secret_store']={'address':'https://secrets.example','token_file':str(self.token),'timeout_seconds':.1}
        self.row['secret_ref']={'mount':'secret','path':'model','field':'value','version':1}
        self.save();self.calls=[];self.status=200;self.version=1;self.value=self.raw.decode()
        async def respond(request):
            self.calls.append(request)
            return httpx.Response(self.status,json={'data':{'data':{'value':self.value},
                'metadata':{'version':self.version,'destroyed':False,'deletion_time':''}}})
        actual=OpenBaoSecrets
        override=patch('app.runtime.openbao_secrets.OpenBaoSecrets',side_effect=lambda cfg:actual(cfg,transport=httpx.MockTransport(respond)))
        override.start();self.addCleanup(override.stop)

    def test_private_owner_and_container_readable_file_modes(self):
        for mode in (0o400,0o600,0o444,0o644):
            self.path.chmod(mode);self.assertTrue(self.resolve()[0].read_only)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_DIRECTORY_NOT_PRIVATE'):self.resolve()
        self.path.chmod(0o600);self.assertEqual(len(self.resolve()),1)
        self.root.chmod(0o777)
        with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_DIRECTORY_NOT_PRIVATE'):self.resolve()

    def test_writable_executable_symlink_fifo_and_oversized_files_denied(self):
        for mode in (0o666,0o620,0o700,0o4600):
            self.path.chmod(mode)
            with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_PERMISSIONS_INVALID'):self.resolve()
        self.path.chmod(0o600)
        link=self.root/'link';link.symlink_to(self.path)
        with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_PATH_INVALID'):private_file(link,16384)
        fifo=self.root/'fifo';os.mkfifo(fifo,0o600)
        with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_PATH_INVALID'):private_file(fifo,16384)
        self.path.write_bytes(b'x'*16385)
        with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_SIZE_INVALID'):self.resolve()

    def test_runtime_uid_must_be_able_to_read_private_file(self):
        mounts=self.resolve();validate_worker_mounts(mounts,f'{os.getuid()}:{os.getgid()}')
        for user in ('0:0','named-user','99999:99999'):
            with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_RUNTIME_USER_DENIED'):validate_worker_mounts(mounts,user)
        self.path.chmod(0o444);validate_worker_mounts(self.resolve(),'99999:99999')

    def test_native_render_matches_pinned_service_bytes_and_rotated_token(self):
        self.managed();self.resolve()
        self.assertEqual(self.calls[-1].url.params['version'],'1')
        self.token.write_text('rotated-service-token');self.resolve()
        self.assertEqual(self.calls[-1].headers['X-Vault-Token'],'rotated-service-token')
        self.assertEqual(self.path.read_bytes(),self.raw)

    def test_stale_render_cannot_mask_deleted_version_or_service_failure(self):
        self.managed()
        for status,code in ((404,'SECRET_UNAVAILABLE'),(403,'SECRET_PROVIDER_UNAVAILABLE'),(503,'SECRET_PROVIDER_UNAVAILABLE')):
            self.status=status
            with self.assertRaisesRegex(RuntimeErrorCode,code):self.resolve()
            self.assertEqual(self.path.read_bytes(),self.raw)
        self.status=200;self.resolve()

    def test_upstream_and_file_mismatch_and_latest_reference_rejected(self):
        self.managed();self.value='changed'
        with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_CONTENT_CHANGED'):self.resolve()
        self.value=self.raw.decode();self.version=2
        with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_PROVIDER_UNAVAILABLE'):self.resolve()
        self.row['secret_ref']['version']=0;self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_CATALOG_INVALID'):self.resolve()

    def test_rotation_keeps_old_request_from_using_new_version(self):
        self.managed();self.resolve()
        self.row['version']='v2';self.row['secret_ref']['version']=2;self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'SECRET_VERSION_CHANGED'):self.resolve()
        self.requests[0]['version']='v2';self.version=2;self.resolve()
