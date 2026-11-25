"""Installation guards and redacted diagnostics; no daemon or live state changes."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[2]/'deploy/linux/install.py'
spec=importlib.util.spec_from_file_location('dios_linux_install',SOURCE)
install=importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class LinuxInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'installation'
        self.args=argparse.Namespace(root=self.root,name='test',backend_image='backend:tag',frontend_image='frontend:tag',api_port=18001,web_port=18002)

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_installation_never_overwritten(self):
        self.root.mkdir(); marker=self.root/'service.env';marker.write_text('retained-secret')
        with patch.dict(os.environ,{},clear=True), self.assertRaisesRegex(install.InstallError,'ALREADY_EXISTS'):
            install.init(self.args)
        self.assertEqual(marker.read_text(),'retained-secret')

    def test_dependency_failure_does_not_create_installation(self):
        with patch.dict(os.environ,{},clear=True), patch.object(install,'command',side_effect=install.InstallError('DEPENDENCY_FAILED:docker')), self.assertRaises(install.InstallError):
            install.init(self.args)
        self.assertFalse(self.root.exists())

    def test_image_tags_are_resolved_to_immutable_ids(self):
        with patch.object(install,'command',return_value='sha256:'+'a'*64):
            self.assertEqual(install.locked_image('mutable:tag'),'sha256:'+'a'*64)
        with patch.object(install,'command',return_value='mutable:tag'), self.assertRaisesRegex(install.InstallError,'IMAGE_ID_INVALID'):
            install.locked_image('mutable:tag')

    def test_private_files_are_exclusive_and_owner_only(self):
        self.root.mkdir();path=self.root/'secret'
        install.private_write(path,'first')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):install.private_write(path,'second')
        self.assertEqual(path.read_text(),'first')

    def test_doctor_reports_path_and_overrides_without_credentials(self):
        self.root.mkdir();(self.root/'config').mkdir()
        install.private_write(self.root/'.env',f'DIOS_ROOT={self.root}\nDIOS_API_PORT=18001\n')
        install.private_write(self.root/'service.env','DIOS_ACCESS_TOKEN='+('secret'*8)+'\nDIOS_E2AG_MODE=enforce\nDIOS_RUNTIME_CONSOLE_ENABLED=true\nHTTPS_PROXY=https://private:password@proxy.invalid\n')
        install.private_write(self.root/'config/work-policy.json','{}')
        output=io.StringIO()
        with patch.dict(os.environ,{'DIOS_API_PORT':'19000','HTTPS_PROXY':'https://user:secret@host.invalid'},clear=True), patch.object(install.shutil,'which',return_value=None), contextlib.redirect_stdout(output):
            code=install.doctor(argparse.Namespace(root=self.root,running=False))
        self.assertEqual(code,1)
        data=json.loads(output.getvalue());checks={r['name']:r for r in data['checks']}
        self.assertFalse(checks['docker_path']['ok']);self.assertFalse(checks['compose_environment']['ok'])
        for sensitive in ('secret','password','proxy.invalid','host.invalid'):
            self.assertNotIn(sensitive,output.getvalue())

    def test_worker_unit_requires_explicit_python_and_remote_docker_is_rejected(self):
        with self.assertRaisesRegex(install.InstallError,'WORKER_PYTHON_MISSING'):
            install.worker_unit(argparse.Namespace(root=self.root,backend=self.root/'backend'))
        with patch.dict(os.environ,{'DOCKER_HOST':'tcp://remote:2375'},clear=True), self.assertRaisesRegex(install.InstallError,'LOCAL_DOCKER_ENDPOINT_REQUIRED'):
            install.init(self.args)
