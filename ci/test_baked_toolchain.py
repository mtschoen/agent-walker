from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.process_safe import run_captured

SCRIPT_PATH = Path(__file__).resolve().parent / "baked-toolchain.sh"


class BakedToolchainTests(unittest.TestCase):
    def run_script(
        self,
        arguments: list[str],
        *,
        image_environment: Path | None = None,
        runner_temp: Path | None = None,
        extra_environment: dict[str, str] | None = None,
    ) -> tuple[int, str, str, str, str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            github_output_path = temporary_path / "github_output"
            github_path_path = temporary_path / "github_path"
            github_output_path.touch()
            github_path_path.touch()

            effective_runner_temp = (
                runner_temp if runner_temp is not None else temporary_path / "runner_temp"
            )
            effective_runner_temp.mkdir(parents=True, exist_ok=True)

            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GITHUB_OUTPUT": str(github_output_path),
                "GITHUB_PATH": str(github_path_path),
                "RUNNER_TEMP": str(effective_runner_temp),
                "TMPDIR": str(effective_runner_temp),
            }
            if image_environment is not None:
                environment["FLEET_IMAGE_ENV"] = str(image_environment)
            else:
                environment["FLEET_IMAGE_ENV"] = str(temporary_path / "missing.env")

            if extra_environment:
                environment.update(extra_environment)

            completed_process = run_captured(
                ["bash", str(SCRIPT_PATH), *arguments],
                timeout=10.0,
                env=environment,
            )

            output_content = github_output_path.read_text(encoding="utf-8")
            path_content = github_path_path.read_text(encoding="utf-8")

            return (
                completed_process.returncode,
                completed_process.stdout,
                completed_process.stderr,
                output_content,
                path_content,
            )

    def test_requires_arguments(self) -> None:
        returncode, _stdout, stderr, _output_content, _path_content = self.run_script([])
        self.assertEqual(returncode, 2)
        self.assertIn("usage: ", stderr)
        self.assertIn("TOOL...", stderr)

    def test_rejects_unknown_toolchain(self) -> None:
        returncode, _stdout, stderr, _output_content, _path_content = self.run_script(["bogus"])
        self.assertEqual(returncode, 2)
        self.assertIn("unknown toolchain: bogus", stderr)

    def test_reports_false_for_go_rust_zig_kcov_outside_fleet_image(self) -> None:
        returncode, _stdout, _stderr, output_content, path_content = self.run_script(
            ["go", "rust", "zig", "kcov"]
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(output_content, "go=false\nrust=false\nzig=false\nkcov=false\n")
        self.assertEqual(path_content, "")

    def test_reports_false_outside_fleet_image(self) -> None:
        returncode, _stdout, _stderr, output_content, path_content = self.run_script(
            ["node", "python", "shellcheck"]
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(output_content, "node=false\npython=false\nshellcheck=false\n")
        self.assertEqual(path_content, "")

    def test_detects_baked_node(self) -> None:
        cases = [
            (
                "matching",
                (
                    "FLEET_NODE_VERSION=22.18.0\n"
                    "function /opt/fleet/node-22.18.0/bin/node() {\n"
                    "  printf 'v22.18.0\\n'\n"
                    "  return 0\n"
                    "}\n"
                ),
                "node=true\n",
                "/opt/fleet/node-22.18.0/bin\n",
            ),
            (
                "mismatched",
                (
                    "FLEET_NODE_VERSION=22.18.0\n"
                    "function /opt/fleet/node-22.18.0/bin/node() {\n"
                    "  printf 'v20.0.0\\n'\n"
                    "  return 0\n"
                    "}\n"
                ),
                "node=false\n",
                "",
            ),
            (
                "missing",
                "FLEET_NODE_VERSION=99.99.99\n",
                "node=false\n",
                "",
            ),
            (
                "nonzero_exit",
                (
                    "FLEET_NODE_VERSION=22.18.0\n"
                    "function /opt/fleet/node-22.18.0/bin/node() {\n"
                    "  printf 'v22.18.0\\n'\n"
                    "  return 1\n"
                    "}\n"
                ),
                "node=false\n",
                "",
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            image_environment_path = temporary_path / "image.env"

            for case_name, environment_content, expected_output, expected_path in cases:
                with self.subTest(case_name=case_name):
                    image_environment_path.write_text(environment_content, encoding="utf-8")
                    returncode, _stdout, _stderr, output_content, path_content = self.run_script(
                        ["node"],
                        image_environment=image_environment_path,
                    )
                    self.assertEqual(returncode, 0)
                    self.assertEqual(output_content, expected_output)
                    self.assertEqual(path_content, expected_path)

    def test_detects_baked_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            image_environment_path = temporary_path / "image.env"

            cases = [
                (
                    "missing",
                    "FLEET_PYTHON_VERSION=99.99.99\n",
                    "python=false\n",
                    False,
                ),
                (
                    "mismatch",
                    (
                        "FLEET_PYTHON_VERSION=3.11.9\n"
                        "function /opt/fleet/python-3.11.9/bin/python() {\n"
                        "  printf 'Python 3.10.0\\n'\n"
                        "  return 0\n"
                        "}\n"
                    ),
                    "python=false\n",
                    False,
                ),
                (
                    "failed_probe",
                    (
                        "FLEET_PYTHON_VERSION=3.11.9\n"
                        "function /opt/fleet/python-3.11.9/bin/python() {\n"
                        "  printf 'Python 3.11.9\\n'\n"
                        "  return 1\n"
                        "}\n"
                    ),
                    "python=false\n",
                    False,
                ),
                (
                    "failed_venv",
                    (
                        "FLEET_PYTHON_VERSION=3.11.9\n"
                        "function /opt/fleet/python-3.11.9/bin/python() {\n"
                        '  if [[ "$1" == "--version" ]]; then\n'
                        "    printf 'Python 3.11.9\\n'\n"
                        "    return 0\n"
                        "  fi\n"
                        "  return 1\n"
                        "}\n"
                    ),
                    "python=false\n",
                    False,
                ),
                (
                    "successful_venv",
                    (
                        "FLEET_PYTHON_VERSION=3.11.9\n"
                        "function /opt/fleet/python-3.11.9/bin/python() {\n"
                        '  if [[ "$1" == "--version" ]]; then\n'
                        "    printf 'Python 3.11.9\\n'\n"
                        "    return 0\n"
                        "  fi\n"
                        f'  command "{sys.executable}" "$@"\n'
                        "}\n"
                    ),
                    "python=true\n",
                    True,
                ),
            ]

            for case_name, environment_content, expected_output, is_successful in cases:
                with self.subTest(case_name=case_name):
                    runner_temp_path = temporary_path / f"runner_temp_{case_name}"
                    runner_temp_path.mkdir(parents=True, exist_ok=True)
                    image_environment_path.write_text(environment_content, encoding="utf-8")

                    returncode, _stdout, _stderr, output_content, path_content = self.run_script(
                        ["python"],
                        image_environment=image_environment_path,
                        runner_temp=runner_temp_path,
                    )
                    self.assertEqual(returncode, 0)
                    self.assertEqual(output_content, expected_output)

                    if is_successful:
                        expected_bin_directory = runner_temp_path / "baked-python" / "bin"
                        self.assertEqual(path_content, f"{expected_bin_directory}\n")
                        virtual_environment_interpreter = expected_bin_directory / "python"
                        self.assertTrue(virtual_environment_interpreter.is_file())
                        self.assertTrue(os.access(virtual_environment_interpreter, os.X_OK))

                        probe_process = run_captured(
                            [
                                str(virtual_environment_interpreter),
                                "-c",
                                "import sys; print(sys.prefix != sys.base_prefix)",
                            ],
                            timeout=10.0,
                        )
                        self.assertEqual(probe_process.returncode, 0)
                        self.assertEqual(probe_process.stdout.strip(), "True")
                    else:
                        self.assertEqual(path_content, "")

    def test_detects_baked_shellcheck(self) -> None:
        cases = [
            ("matching", "0", 0, "shellcheck=true\n"),
            ("mismatch", "1", 0, "shellcheck=false\n"),
            ("nonzero_exit", "0", 1, "shellcheck=false\n"),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            image_environment_path = temporary_path / "image.env"

            fake_bin_directory = temporary_path / "bin"
            fake_bin_directory.mkdir()
            fake_shellcheck = fake_bin_directory / "shellcheck"
            fake_shellcheck.write_text(
                '#!/bin/sh\n'
                'echo "version: 0.10.0"\n'
                'exit "${FAKE_SHELLCHECK_EXIT:-0}"\n',
                encoding="utf-8",
            )
            fake_shellcheck.chmod(fake_shellcheck.stat().st_mode | stat.S_IXUSR)

            for case_name, major_version, exit_code, expected_output in cases:
                with self.subTest(case_name=case_name):
                    image_environment_path.write_text(
                        f"SHELLCHECK_MAJOR={major_version}\n",
                        encoding="utf-8",
                    )
                    returncode, _stdout, _stderr, output_content, path_content = self.run_script(
                        ["shellcheck"],
                        image_environment=image_environment_path,
                        extra_environment={
                            "PATH": f"{fake_bin_directory}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                            "FAKE_SHELLCHECK_EXIT": str(exit_code),
                        },
                    )
                    self.assertEqual(returncode, 0)
                    self.assertEqual(output_content, expected_output)
                    self.assertEqual(path_content, "")


if __name__ == "__main__":
    unittest.main()
