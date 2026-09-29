import locale
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from clipsave_app import __version__
from clipsave_app.constants import APP_VERSION


class ReleaseContractTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('pwsh'), 'PowerShell 7 is required')
    def test_only_explicit_unpublished_drafts_can_be_rebuilt(self):
        workflow = Path('.github/workflows/release.yml').read_text(encoding='utf-8')
        step = workflow.split('      - name: Resolve release tag', 1)[1].split('      - name: Checkout tagged source', 1)[0]
        resolver = textwrap.dedent(step.split('        run: |\n', 1)[1])
        # GitHub expands these before PowerShell parses the step.
        resolver = re.sub(r'\$\{\{.*?\}\}', 'test', resolver)
        mock = '''
$ErrorActionPreference = 'Stop'
function Invoke-WebRequest {
    param($Headers, $Uri, [switch]$SkipHttpErrorCheck)
    if ($Uri.Contains('/git/ref/tags/')) {
        return [pscustomobject]@{StatusCode=200; Content='{}'}
    }
    return [pscustomobject]@{StatusCode=[int]$env:MOCK_STATUS; Content=$env:MOCK_RELEASE}
}
'''
        for payload, replace, success, release, replaced in (
            ('[{"tag_name":"v1.1.5","draft":true}]', True, True, True, True),
            ('[{"tag_name":"v1.1.5","draft":false}]', True, False, False, False),
            ('[{"tag_name":"v1.1.5","draft":true}]', False, True, False, False),
            ('[]', False, True, True, False),
            ('[{"tag_name":"v1.1.5","draft":true},{"tag_name":"v1.1.5","draft":true}]', True, False, False, False),
        ):
            with self.subTest(payload=payload, replace=replace), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                script = root / 'resolve.ps1'
                script.write_text(mock + resolver, encoding='utf-8')
                output = root / 'output.txt'
                env = dict(os.environ, INPUT_TAG='v1.1.5', REPLACE_DRAFT=str(replace).lower(),
                           MOCK_STATUS='200', MOCK_RELEASE=payload,
                           GITHUB_OUTPUT=str(output))
                result = subprocess.run([shutil.which('pwsh'), '-NoProfile', '-File', str(script)],
                                        env=env, capture_output=True, timeout=30)
                self.assertEqual(result.returncode == 0, success, result.stderr.decode(errors='replace'))
                flags = output.read_text(encoding='utf-8-sig') if output.exists() else ''
                self.assertEqual('should_release=true' in flags, release)
                self.assertEqual('replace_draft=true' in flags, replaced)

    def test_release_stays_draft_until_desktop_gates_pass(self):
        workflow = Path('.github/workflows/release.yml').read_text(encoding='utf-8')
        publish = next(line for line in workflow.splitlines() if 'gh release create ' in line)
        self.assertIn('--draft', publish)
        self.assertNotIn('--latest', publish)

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

    def test_release_notes_do_not_hardcode_a_previous_release_theme(self):
        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertNotIn(
            "focuses on Windows Acrylic quality and interactive window performance",
            workflow,
        )
        self.assertIn("$($match.Groups[1].Value.Trim())", workflow)

    def test_release_validates_pyinstaller_warnings_before_publish(self):
        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
        warning_gate = (
            ".\\.venv\\Scripts\\python.exe check_pyinstaller_warnings.py "
            "build\\work\\ClipSave\\warn-ClipSave.txt"
        )
        self.assertIn(warning_gate, workflow)
        self.assertLess(workflow.index(warning_gate), workflow.index("Publish GitHub Release"))

    def test_release_checklist_owns_windows_visual_and_interactive_gates(self):
        checklist = Path("RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
        self.assertIn("verify_windows_visual_smoke.py", checklist)
        self.assertIn("verify_windows_interactive_backdrop.py", checklist)
        self.assertIn("--expected-backend win10_effect_acrylic", checklist)
        self.assertIn("resting_backend=win10_effect_acrylic", checklist)
        self.assertIn("geometry_lock: max_delta=0px", checklist)
        self.assertIn("Do not publish a release", checklist)

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
            r'call fake-python\.bat -c "[^"\r\n]*"',
            "call fake-python.bat",
            instrumented,
        )
        instrumented = instrumented.replace(
            'if not exist "call fake-python.bat"',
            'if not exist ".venv\\Scripts\\python.exe"',
        )
        instrumented = instrumented.replace("`call fake-python.bat", "`fake-python.bat")
        instrumented = instrumented.replace("powershell ", "call fake-powershell.bat ")
        instrumented = instrumented.replace(
            '"%installerCompiler%" /Qp', 'call "%installerCompiler%" /Qp'
        )

        (root / ".venv" / "Scripts").mkdir(parents=True)
        (root / ".venv" / "Scripts" / "python.exe").touch()
        (root / "build.bat").write_text(instrumented, encoding="ascii")
        (root / ".gitignore").write_text("build/\n", encoding="ascii")
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "README_RELEASE.md"):
            (root / name).write_text(name + "\n", encoding="ascii")

        (root / "fake-python.bat").write_text(
            "@echo off\n"
            "if \"%1\"==\"build_windows_backdrop.py\" (\n"
            "  mkdir build\\windows_backdrop\\runtime >nul 2>nul\n"
            "  >build\\windows_backdrop\\ClipSave.manifest echo manifest\n"
            "  type nul > build\\windows_backdrop\\runtime\\clipsave_windows_backdrop.dll\n"
            "  type nul > build\\windows_backdrop\\runtime\\Microsoft.Graphics.Canvas.dll\n"
            "  type nul > build\\windows_backdrop\\runtime\\msvcp140_app.dll\n"
            "  type nul > build\\windows_backdrop\\runtime\\vcruntime140_1_app.dll\n"
            "  type nul > build\\windows_backdrop\\runtime\\vcruntime140_app.dll\n"
            ")\n"
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
        (root / "fake-iscc.bat").write_text(
            "@echo off\n"
            "if not exist build\\release mkdir build\\release\n"
            "type nul > build\\release\\ClipSave-1.2.3-UNOFFICIAL-windows-x64-installer.exe\n"
            "type nul > build\\release\\ClipSave-1.2.3-windows-x64-installer.exe\n"
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
        env["INNO_SETUP_COMPILER"] = str(root / "fake-iscc.bat")
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
            encoding=locale.getencoding(),
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
