from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from docker.errors import ImageNotFound

from app.config import settings
from app.services import docker_runner


class DockerRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_client = docker_runner._client
        self.original_image = settings.diagent_image
        docker_runner._client = None
        settings.diagent_image = "missing-diagent:test"

    def tearDown(self) -> None:
        docker_runner._client = self.original_client
        settings.diagent_image = self.original_image

    @patch("app.services.docker_runner.docker.from_env")
    def test_missing_task_image_fails_without_implicit_pull(self, from_env: MagicMock) -> None:
        client = MagicMock()
        client.images.get.side_effect = ImageNotFound("missing")
        from_env.return_value = client

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "DIAGENT_IMAGE_NOT_PRESENT"):
                docker_runner.start_container("run-1", Path(directory))

        from_env.assert_called_once_with(timeout=30)
        client.containers.run.assert_not_called()

    def test_container_logs_are_decoded_and_bounded(self) -> None:
        container = MagicMock()
        container.logs.return_value = "诊断日志".encode("utf-8")
        client = MagicMock()
        client.containers.get.return_value = container
        docker_runner._client = client

        self.assertEqual(
            docker_runner.get_container_logs("container-id", tail=17),
            "诊断日志",
        )
        container.logs.assert_called_once_with(stdout=True, stderr=True, tail=17)

    def test_task_selection_requires_a_compatible_image_and_uses_the_generated_config(self) -> None:
        client = MagicMock()
        image = client.images.get.return_value
        image.labels = {}
        docker_runner._client = client
        files = {"task.json": {"task": {"tool_selection": {"tool_ids": []}}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "does not support tool_selection"):
                docker_runner.start_container("run-1", Path(directory), task_files=files)
            client.containers.run.assert_not_called()
            client.containers.create.assert_not_called()

            image.labels = {"org.hqit.diagent.task-tool-selection": "1"}
            image.id = "sha256:fixture-task-image"
            docker_runner.start_container("run-1", Path(directory), {"TASK_CONFIG": "/workspace/alternate.json"},
                                          task_files=files)

        args, options = client.containers.create.call_args
        self.assertEqual(args, (image.id,))
        self.assertEqual(options["environment"]["TASK_CONFIG"], "/run/diagent/task.json")
        container = client.containers.create.return_value
        destination, archive = container.put_archive.call_args.args
        self.assertEqual(destination, "/run/diagent")
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            self.assertEqual(bundle.getmember("task.json").mode, 0o400)
            self.assertEqual(json.load(bundle.extractfile("task.json")), files["task.json"])
        container.start.assert_called_once_with()

    def test_named_subagents_require_the_task_image_capability_before_launch(self) -> None:
        client = MagicMock()
        client.images.get.return_value.labels = {"org.hqit.diagent.task-tool-selection": "1"}
        docker_runner._client = client
        files = {"task.json": {"task": {"subagents": [{
            "name": "reviewer", "tool_selection": {"tool_ids": []},
        }]}}}

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "does not support DSH subagents"):
                docker_runner.start_container("run-1", Path(directory), task_files=files)
        client.containers.create.assert_not_called()

    @patch("app.services.docker_runner.docker.from_env")
    def test_task_container_joins_configured_network(self, from_env: MagicMock) -> None:
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        client.containers.run.return_value.id = "container-id"
        from_env.return_value = client
        original_network = settings.docker_network
        settings.docker_network = "e2ag-test-network"
        try:
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(
                    docker_runner.start_container("run-2", Path(directory)),
                    "container-id",
                )
        finally:
            settings.docker_network = original_network

        self.assertEqual(
            client.containers.run.call_args.kwargs["network"],
            "e2ag-test-network",
        )


if __name__ == "__main__":
    unittest.main()
