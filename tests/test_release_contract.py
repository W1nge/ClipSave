import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from clipsave_app import __version__
from clipsave_app.constants import APP_VERSION


class ReleaseContractTests(unittest.TestCase):
    def test_public_version_has_single_source(self):
        self.assertEqual(__version__, APP_VERSION)
        init_text = Path("clipsave_app/__init__.py").read_text(encoding="utf-8")
        self.assertNotRegex(init_text, r'__version__\s*=\s*["\']')
        release_readme = Path("README_RELEASE.md").read_text(encoding="utf-8")
        self.assertNotIn(f"ClipSave {APP_VERSION}", release_readme)

    def test_unofficial_build_uses_distinct_label_and_archive_name(self):
        script = Path("build.bat").read_text(encoding="utf-8")
        self.assertIn('set "buildLabel=UNOFFICIAL - local or unverified build"', script)
        self.assertIn('set "archiveLabel=-UNOFFICIAL"', script)
        self.assertIn("ClipSave-%appVersion%%archiveLabel%-windows-x64.zip", script)
        self.assertIn(
            "ClipSave-%appVersion%%archiveLabel%-windows-x64-installer.exe",
            script,
        )
        self.assertIn("Build channel %buildLabel%", script)

    def test_installer_is_per_user_and_keeps_data_outside_program_directory(self):
        installer = Path("installer.iss").read_text(encoding="ascii")
        self.assertIn("PrivilegesRequired=lowest", installer)
        self.assertIn("DefaultDirName={localappdata}\\Programs\\ClipSave", installer)
        self.assertIn('Source: "build\\release\\ClipSave\\*"', installer)
        self.assertNotIn("{localappdata}\\ClipSave", installer)

    def test_official_build_revalidates_source_before_release_metadata(self):
        script = Path("build.bat").read_text(encoding="ascii")
        initial_clean = script.index(
            "Official releases require a clean Git working tree."
        )
        captured_head = script.index('set "officialHead=%%C"')
        late_check = script.index("call :verify_official_source")
        official_label = script.index('set "buildLabel=OFFICIAL"')
        build_info = script.index('>"%releaseDir%\\BUILD_INFO.txt"')

        self.assertLess(initial_clean, captured_head)
        self.assertLess(captured_head, late_check)
        self.assertLess(late_check, official_label)
        self.assertLess(official_label, build_info)
        self.assertIn('if not "%currentOfficialHead%"=="%officialHead%"', script)
        self.assertIn("Official release source became dirty during the build", script)

    @unittest.skipUnless(os.name == "nt", "build.bat behavior requires Windows")
    def test_official_build_cleans_release_when_head_changes_mid_build(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._prepare_instrumented_build(root)
            completed = self._run_instrumented_build(root, "head", official=True)

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn(
                "Official release source changed during the build", completed.stdout
            )
            self.assertFalse((root / "build" / "release").exists())
            self.assertNotIn("Build channel OFFICIAL", completed.stdout)

    @unittest.skipUnless(os.name == "nt", "build.bat behavior requires Windows")
    def test_official_build_cleans_release_when_tree_becomes_dirty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._prepare_instrumented_build(root)
            completed = self._run_instrumented_build(root, "dirty", official=True)

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn(
                "Official release source became dirty during the build",
                completed.stdout,
            )
            self.assertFalse((root / "build" / "release").exists())
            self.assertNotIn("Build channel OFFICIAL", completed.stdout)

    @unittest.skipUnless(os.name == "nt", "build.bat behavior requires Windows")
    def test_nonofficial_build_keeps_dirty_source_behavior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._prepare_instrumented_build(root)
            completed = self._run_instrumented_build(root, "dirty", official=False)

            self.assertEqual(completed.returncode, 0, completed.stdout)
            build_info = (root / "build" / "release" / "BUILD_INFO.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("Build channel UNOFFICIAL - local or unverified build", build_info)
            self.assertRegex(build_info, r"Commit .*?-dirty")
            self.assertIn("-UNOFFICIAL-windows-x64.zip", completed.stdout)

    def test_release_uses_the_application_executable_without_legacy_launchers(self):
        script = Path("build.bat").read_text(encoding="ascii")
        release_readme = Path("README_RELEASE.md").read_text(encoding="utf-8")
        self.assertFalse(Path("run.vbs").exists())
        self.assertFalse(Path("双击启动.vbs").exists())
        self.assertNotIn("*.vbs", script)
        self.assertIn("ClipSave\\ClipSave.exe", release_readme)

    def test_ci_pins_runner_and_waits_before_second_instance(self):
        workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertNotIn("windows-latest", workflow)
        self.assertGreaterEqual(workflow.count("runs-on: windows-2022"), 2)
        first_ready_check = workflow.index("First ClipSave instance did not report ready state before contention test")
        second_start = workflow.index("$second = Start-Process", first_ready_check)
        self.assertLess(first_ready_check, second_start)
        self.assertIn("'--smoke-hold-ms', '10000'", workflow)

    def test_documentation_distinguishes_integrity_from_authenticity(self):
        release_readme = Path("README_RELEASE.md").read_text(encoding="utf-8")
        self.assertIn("integrity checks only", release_readme)
        self.assertIn("do not authenticate", release_readme)

    def _prepare_instrumented_build(self, root: Path) -> None:
        source = Path("build.bat").read_text(encoding="ascii")
        instrumented = source.replace(
            ".venv\\Scripts\\python.exe", "call fake-python.bat"
        )
        instrumented = re.sub(
            r'call fake-python\.bat -c ".*"',
            "call fake-python.bat",
            instrumented,
        )
        instrumented = instrumented.replace(
            'if not exist "call fake-python.bat"',
            'if not exist ".venv\\Scripts\\python.exe"',
        )
        instrumented = instrumented.replace("`call fake-python.bat", "`fake-python.bat")
        instrumented = instrumented.replace("powershell ", "call fake-powershell.bat ")

        (root / ".venv" / "Scripts").mkdir(parents=True)
        (root / ".venv" / "Scripts" / "python.exe").touch()
        (root / "build.bat").write_text(instrumented, encoding="ascii")
        (root / ".gitignore").write_text("build/\n", encoding="ascii")
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "README_RELEASE.md"):
            (root / name).write_text(name + "\n", encoding="ascii")

        (root / "fake-python.bat").write_text(
            "@echo off\n"
            "if \"%1\"==\"-m\" if \"%2\"==\"PyInstaller\" (\n"
            "  mkdir build\\release\\ClipSave\\_internal >nul 2>nul\n"
            "  type nul > build\\release\\ClipSave\\ClipSave.exe\n"
            "  if /i \"%SOURCE_MUTATION%\"==\"dirty\" type nul > source-mutated.flag\n"
            "  if /i \"%SOURCE_MUTATION%\"==\"head\" "
            "git commit --allow-empty -m mid-build >nul 2>nul\n"
            ")\n"
            "if \"%1\"==\"collect_third_party_licenses.py\" "
            "mkdir \"%~2\" >nul 2>nul\n"
            "if \"%1\"==\"build_manifest.py\" >\"%~3\" echo manifest\n"
            "echo 1.2.3\n"
            "exit /b 0\n",
            encoding="ascii",
        )
        (root / "fake-powershell.bat").write_text("@exit /b 0\n", encoding="ascii")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "build-tests@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Build Tests"], cwd=root, check=True
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"], cwd=root, check=True
        )

    def _run_instrumented_build(
        self, root: Path, mutation: str, *, official: bool
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
        env["SOURCE_MUTATION"] = mutation
        if official:
            env["CLIPSAVE_OFFICIAL_BUILD"] = "1"
        else:
            env.pop("CLIPSAVE_OFFICIAL_BUILD", None)
        return subprocess.run(
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


if __name__ == "__main__":
    unittest.main()
