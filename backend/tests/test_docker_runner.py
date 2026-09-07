from __future__ import annotations

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
