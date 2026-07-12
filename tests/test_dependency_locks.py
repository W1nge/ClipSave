import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from packaging.requirements import Requirement


HASH_PATTERN = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def locked_requirements(path: Path) -> dict[str, tuple[str, list[str]]]:
    logical_lines = path.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
    result = {}
    for line in logical_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line.split("--hash=", 1)[0].strip())
        hashes = HASH_PATTERN.findall(line)
        version = next(iter(requirement.specifier)).version
        result[requirement.name.lower()] = (version, hashes)
    return result


class DependencyLockTests(unittest.TestCase):
    def test_runtime_lock_matches_declared_requirements_and_has_valid_hashes(self):
        declared = {
            Requirement(line).name.lower(): next(iter(Requirement(line).specifier)).version
            for line in Path("requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        locked = locked_requirements(Path("requirements-windows.lock"))
        self.assertEqual({name: value[0] for name, value in locked.items()}, declared)
        self.assertTrue(all(hashes for _version, hashes in locked.values()))

    def test_build_lock_matches_declared_requirements_and_has_valid_hashes(self):
        declared = {
            Requirement(line).name.lower(): next(iter(Requirement(line).specifier)).version
            for line in Path("build-requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        locked = locked_requirements(Path("build-requirements-windows.lock"))
        self.assertEqual({name: value[0] for name, value in locked.items()}, declared)
        self.assertTrue(all(hashes for _version, hashes in locked.values()))

    def test_build_script_enforces_runtime_and_build_hash_locks(self):
        script = Path("build.bat").read_text(encoding="utf-8")
        runtime_command = (
            "pip install %lockedInstallOptions% --require-hashes "
            "-r requirements-windows.lock"
        )
        build_command = (
            "pip install %lockedInstallOptions% --require-hashes "
            "-r build-requirements-windows.lock"
        )

        self.assertIn(runtime_command, script)
        self.assertIn(build_command, script)
        self.assertLess(script.index(runtime_command), script.index(build_command))
        force_assignment = 'set "lockedInstallOptions=--force-reinstall"'
        self.assertIn(
            'if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" ' + force_assignment,
            script,
        )
        self.assertLess(script.index(force_assignment), script.index(runtime_command))
        self.assertLess(script.index(build_command), script.index("pip check"))
        self.assertNotIn("Python executable ' + sys.executable", script)
        self.assertIn("sys.version_info[:3] == (3,13,5)", script)
        self.assertIn("Official releases require a clean Git working tree", script)
        self.assertIn("Official build environment has undeclared distributions", script)
        self.assertIn("allowed={'pip'}", script)
        self.assertIn("README_RELEASE.md", script)

    def test_ci_matrix_installs_hash_locked_test_dependency(self):
        workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
        matrix_job = workflow.split("  package:", 1)[0]

        self.assertIn(
            "python -m pip install --require-hashes -r requirements-windows.lock",
            matrix_job,
        )
        self.assertIn(
            "python -m pip install --require-hashes "
            "-r build-requirements-windows.lock",
            matrix_job,
        )

    @unittest.skipUnless(os.name == "nt", "build.bat behavior requires Windows")
    def test_official_build_force_reinstalls_both_locks(self):
        source = Path("build.bat").read_text(encoding="utf-8")
        instrumented = source.replace(
            ".venv\\Scripts\\python.exe", "call fake-python.bat"
        )
        instrumented = instrumented.replace(
            'if not exist "call fake-python.bat"',
            'if not exist ".venv\\Scripts\\python.exe"',
        )
        instrumented = instrumented.replace("git ", "call fake-git.bat ")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".venv" / "Scripts").mkdir(parents=True)
            (root / ".venv" / "Scripts" / "python.exe").touch()
            (root / "build.bat").write_text(instrumented, encoding="ascii")
            (root / "fake-python.bat").write_text(
                "@echo off\n"
                ">>\"%FAKE_PYTHON_LOG%\" echo(%*\n"
                "echo(%*| %SystemRoot%\\System32\\findstr.exe "
                "/c:\"build-requirements-windows.lock\" >nul\n"
                "if not errorlevel 1 exit /b 23\n"
                "exit /b 0\n",
                encoding="ascii",
            )
            (root / "fake-git.bat").write_text(
                "@echo off\n"
                "if \"%1\"==\"rev-parse\" "
                "echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                "exit /b 0\n",
                encoding="ascii",
            )

            official_calls = self._run_instrumented_build(root, official=True)
            nonofficial_calls = self._run_instrumented_build(root, official=False)

        official_installs = [line for line in official_calls if "-m pip install" in line]
        nonofficial_installs = [
            line for line in nonofficial_calls if "-m pip install" in line
        ]
        self.assertEqual(len(official_installs), 2)
        self.assertEqual(len(nonofficial_installs), 2)
        self.assertTrue(all("--force-reinstall" in line for line in official_installs))
        self.assertTrue(
            all("--force-reinstall" not in line for line in nonofficial_installs)
        )

    def _run_instrumented_build(self, root: Path, *, official: bool) -> list[str]:
        log_path = root / ("official.log" if official else "nonofficial.log")
        env = os.environ.copy()
        env["FAKE_PYTHON_LOG"] = str(log_path)
        env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
        if official:
            env["CLIPSAVE_OFFICIAL_BUILD"] = "1"
        else:
            env.pop("CLIPSAVE_OFFICIAL_BUILD", None)

        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "build.bat"],
            cwd=root,
            env=env,
            input="\n",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 23, completed.stdout)
        return log_path.read_text(encoding="ascii").splitlines()

    def test_native_runtime_license_texts_are_present(self):
        for name in ("OpenSSL-Apache-2.0.txt", "SQLite-Public-Domain.txt"):
            with self.subTest(name=name):
                path = Path("third_party_licenses") / name
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
