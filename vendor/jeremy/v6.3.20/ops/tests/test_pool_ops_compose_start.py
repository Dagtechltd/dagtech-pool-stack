#!/usr/bin/env python3

import os
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class ComposeStartCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_stack_services = list(pool_ops.STACK_SERVICES)
        self.original_project_root = pool_ops.PROJECT_ROOT
        self.original_pool_env = pool_ops.POOL_ENV_FILE
        self.original_env = dict(os.environ)
        self.original_docker_sudo_cache = pool_ops._DOCKER_USE_SUDO_CACHE
        self.original_compose_container_cache = dict(pool_ops._COMPOSE_CONTAINER_NAME_CACHE)
        pool_ops._COMPOSE_CONTAINER_NAME_CACHE.clear()
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.STACK_SERVICES = self.original_stack_services
        pool_ops.PROJECT_ROOT = self.original_project_root
        pool_ops.POOL_ENV_FILE = self.original_pool_env
        pool_ops._DOCKER_USE_SUDO_CACHE = self.original_docker_sudo_cache
        pool_ops._COMPOSE_CONTAINER_NAME_CACHE.clear()
        pool_ops._COMPOSE_CONTAINER_NAME_CACHE.update(self.original_compose_container_cache)
        os.environ.clear()
        os.environ.update(self.original_env)

    def fake_inspect(self, labels: dict[str, str]):
        def run(command, **_kwargs):
            name = command[-1]
            service = labels.get(name)
            if service is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            return SimpleNamespace(returncode=0, stdout=f"{service}\n", stderr="")

        return run

    def test_repair_start_command_uses_configured_core_services(self) -> None:
        os.environ["BDAG_START_SERVICES"] = "postgres,node,pool"
        command = pool_ops.docker_compose_start_command()

        self.assertEqual(command[-5:], ["up", "-d", "postgres", "node", "pool"])
        self.assertNotIn("hotsnap", command)
        self.assertNotIn("snapshot-node", command)

    def test_repair_start_command_excludes_pool_dependencies_when_pool_is_unsafe(self) -> None:
        os.environ["BDAG_START_SERVICES"] = "postgres,node,pool"
        command = pool_ops.docker_compose_start_command(include_pool=False)

        self.assertEqual(command[-5:], ["up", "-d", "--no-deps", "postgres", "node"])
        self.assertNotIn("pool", command[-2:])

    def test_repair_start_command_infers_service_from_compose_container_name(self) -> None:
        os.environ.pop("BDAG_START_SERVICES", None)
        pool_ops.STACK_SERVICES = ["pool-stack-docker-node-1"]
        with mock.patch.object(
            pool_ops.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="not found"),
        ):
            command = pool_ops.docker_compose_start_command()

        self.assertEqual(command[-3:], ["up", "-d", "node"])

    def test_inspect_timeout_falls_back_to_compose_container_name(self) -> None:
        os.environ.pop("BDAG_START_SERVICES", None)
        pool_ops.STACK_SERVICES = ["pool-stack-docker-node-1"]
        with mock.patch.object(pool_ops.subprocess, "run", side_effect=pool_ops.subprocess.TimeoutExpired("docker", 10)):
            command = pool_ops.docker_compose_start_command()

        self.assertEqual(command[-3:], ["up", "-d", "node"])

    def test_docker_run_retries_with_noninteractive_sudo_on_access_error(self) -> None:
        pool_ops._DOCKER_USE_SUDO_CACHE = None
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command[0] == "docker":
                return SimpleNamespace(
                    returncode=1,
                    stdout=b"",
                    stderr=b"permission denied while trying to connect to the Docker API",
                )
            return SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")

        with mock.patch.object(pool_ops.subprocess, "run", side_effect=fake_run):
            result = pool_ops.run(["docker", "ps"], timeout=1)

        self.assertTrue(result.ok)
        self.assertEqual(calls, [["docker", "ps"], ["sudo", "-n", "docker", "ps"]])
        self.assertIs(pool_ops._DOCKER_USE_SUDO_CACHE, True)

    def test_compose_container_name_does_not_cache_boot_miss(self) -> None:
        os.environ["BDAG_COMPOSE_PROJECT_NAME"] = "stack"
        calls = []
        project_queries_per_lookup = len(
            pool_ops.unique_names([pool_ops.docker_compose_project_name(), "pool-stack-docker"])
        )

        def fake_run(command, timeout=20):  # noqa: ARG001
            calls.append(command)
            stdout = "" if len(calls) <= project_queries_per_lookup else "stack-node-1\tUp 2 seconds\n"
            return pool_ops.CommandResult(command=list(command), returncode=0, stdout=stdout, stderr="", elapsed=0.0)

        with mock.patch.object(pool_ops, "run", side_effect=fake_run):
            self.assertEqual(pool_ops.compose_container_name("node"), "node")
            self.assertEqual(pool_ops.compose_container_name("node"), "stack-node-1")

        self.assertEqual(len(calls), project_queries_per_lookup + 1)

    def test_docker_inspect_re_resolves_stale_cached_container_name(self) -> None:
        pool_ops._COMPOSE_CONTAINER_NAME_CACHE["node"] = "old-node-1"
        calls = []

        def fake_run(command, timeout=20):  # noqa: ARG001
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return pool_ops.CommandResult(
                    command=list(command),
                    returncode=0,
                    stdout="stack-node-1\tUp 2 seconds\n",
                    stderr="",
                    elapsed=0.0,
                )
            if command[:2] == ["docker", "inspect"] and command[-1] == "stack-node-1":
                return pool_ops.CommandResult(
                    command=list(command),
                    returncode=0,
                    stdout=(
                        '[{"Name":"/stack-node-1","State":{"Running":true,"Status":"running"},'
                        '"Config":{"Image":"stack-node","Labels":{"com.docker.compose.service":"node"}},'
                        '"NetworkSettings":{"Ports":{},"Networks":{}},"RestartCount":0}]'
                    ),
                    stderr="",
                    elapsed=0.0,
                )
            return pool_ops.CommandResult(command=list(command), returncode=1, stdout="", stderr="not found", elapsed=0.0)

        with mock.patch.object(pool_ops, "run", side_effect=fake_run):
            inspected = pool_ops.docker_inspect(["node"])

        self.assertTrue(inspected["node"]["running"])
        self.assertEqual(inspected["node"]["name"], "stack-node-1")
        self.assertEqual(pool_ops._COMPOSE_CONTAINER_NAME_CACHE["node"], "stack-node-1")


if __name__ == "__main__":
    unittest.main()
