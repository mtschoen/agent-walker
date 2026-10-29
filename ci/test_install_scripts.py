import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.process_safe import run_captured  # noqa: E402

# Generous ceiling that only catches a hung shell, never a slow healthy one.
HANG_GUARD_SECONDS = 300.0


class InstallScriptTests(unittest.TestCase):
    def test_posix_installer_adds_search_cli_and_preserves_native_alias(self) -> None:
        script = (ROOT / "install.sh").read_text(encoding="utf-8")

        for expected in (
            "search/session_search.py",
            "search/opencode_search.py",
            "shared/process_safe.py",
            "agent-walker-search",
            "claude-walker",
            "--period 86400 --win-start 0",
            "find_python",
        ):
            self.assertIn(expected, script)
        for removed in ("claude mcp", "mcp~=", ".venv", "register_mcp", "--project"):
            self.assertNotIn(removed, script)

    def _run_installer_functions(
        self,
        command: str,
        path: str,
        working_directory: Path,
        variables: dict[str, str] | None = None,
    ) -> str:
        """Run install.sh's helper functions in bash and return stdout."""
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not available")
        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        sources = []
        for name in (
            "find_python",
            "absolute_path",
            "shell_quote",
            "write_search_launcher",
        ):
            start = script.index(f"{name}() {{")
            sources.append(script[start : script.index("\n}\n", start) + 3])
        completed = run_captured(
            [bash, "-c", "\n".join(sources) + "\n" + command],
            env={**os.environ, **(variables or {}), "PATH": path},
            cwd=str(working_directory),
            timeout=HANG_GUARD_SECONDS,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    @staticmethod
    def _write_stub(path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)

    @unittest.skipUnless(os.name == "posix", "exercises POSIX PATH and exec semantics")
    def test_posix_installer_discovers_an_absolute_supported_python(self) -> None:
        cases = (
            (["python3", "python"], "bin", "python3"),
            (["python"], "bin", "python"),
            (["python3.13"], "bin", "python3.13"),
            (["python"], "relative", "python"),
        )
        for executable_names, layout, expected_name in cases:
            with self.subTest(names=executable_names, layout=layout):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    interpreters = root / "interpreters"
                    for name in executable_names:
                        self._write_stub(interpreters / name, "exit 0")
                    # Only the stubs and the helpers' own tools are on PATH,
                    # so the host's real interpreter cannot win discovery.
                    tools = root / "tools"
                    tools.mkdir()
                    for tool in ("dirname", "basename"):
                        located = shutil.which(tool)
                        if located is None:
                            self.skipTest(f"{tool} is not available")
                        (tools / tool).symlink_to(located)
                    path_entry = (
                        "interpreters" if layout == "relative" else str(interpreters)
                    )
                    discovered = self._run_installer_functions(
                        "find_python", f"{path_entry}:{tools}", root
                    ).strip()
                    self.assertEqual(discovered, str(interpreters / expected_name))

    @unittest.skipUnless(os.name == "posix", "exercises POSIX PATH and exec semantics")
    def test_posix_installer_launcher_passes_paths_literally(self) -> None:
        hostile = 'dir $(touch subst_marker) `touch tick_marker` it\'s "quoted" $USER'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            interpreter = root / hostile / "python3"
            self._write_stub(
                interpreter, 'for argument in "$@"; do echo "$argument"; done'
            )
            script = root / hostile / "agent-walker-search.py"
            script.write_text("", encoding="utf-8")
            launcher = root / "agent-walker-search"
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            output = self._run_installer_functions(
                'write_search_launcher "$LAUNCHER" "$INTERPRETER" "$SCRIPT"',
                os.environ.get("PATH", ""),
                root,
                {
                    "LAUNCHER": str(launcher),
                    "INTERPRETER": str(interpreter),
                    "SCRIPT": str(script),
                },
            )
            self.assertEqual(output, "")
            completed = run_captured(
                [str(launcher), "--query", "x"],
                cwd=str(elsewhere),
                timeout=HANG_GUARD_SECONDS,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                completed.stdout.splitlines(), [str(script), "--query", "x"]
            )
            for marker in ("subst_marker", "tick_marker"):
                self.assertEqual(list(root.rglob(marker)), [])

    def test_windows_installer_adds_search_cli_and_preserves_native_alias(self) -> None:
        script = (ROOT / "install.bat").read_text(encoding="utf-8")

        for expected in (
            "search\\session_search.py",
            "search\\opencode_search.py",
            "shared\\process_safe.py",
            "agent-walker-search.py",
            "agent-walker-search.bat",
            'call "%INSTALL_DIR%\\agent-walker-search.bat" --help',
            "claude-walker",
            "--period 86400 --win-start 0",
        ):
            self.assertIn(expected, script)
        for removed in ("claude mcp", "mcp~=", ".venv", "register_mcp", "--project"):
            self.assertNotIn(removed, script)

    def test_session_search_skill_teaches_the_cli(self) -> None:
        skill_path = ROOT / "skills" / "session-search" / "SKILL.md"
        self.assertTrue(skill_path.is_file())
        skill = skill_path.read_text(encoding="utf-8")

        self.assertIn("name: session-search", skill)
        self.assertIn("description: Use when", skill)
        self.assertIn("earlier conversation", skill)
        self.assertIn("what was decided before", skill)
        self.assertIn("past agent session", skill)
        self.assertIn("agent-walker-search --query", skill)
        self.assertIn("--providers", skill)
        self.assertIn("--limit", skill)
        self.assertIn("--json", skill)


if __name__ == "__main__":
    unittest.main()
