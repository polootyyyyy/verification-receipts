from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verification_receipts.py"


class ReceiptsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="receipt test ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo with spaces"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Receipt Tests")
        self.git("config", "user.email", "tests@example.invalid")
        (self.root / "source.txt").write_text("original\n")
        (self.root / ".gitignore").write_text("ignored/\n")
        self.git("add", "source.txt", ".gitignore")
        self.git("commit", "-m", "Initial source")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def invoke(self, *args, cwd=None, expected=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, text=True)
        self.assertTrue(result.stdout, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(result.returncode, {"PASS_CURRENT": 0, "FAIL_CURRENT": 1, "STALE": 2,
                                           "INVALID": 3, "UNKNOWN": 4}[data["status"]], result.stderr)
        if expected:
            self.assertEqual(data["status"], expected, data)
        return data

    def capture(self, source="print('verified')", expected="PASS_CURRENT", timeout="2", cwd=None):
        return self.invoke("capture", "--timeout", timeout, "--", sys.executable, "-c", source,
                           expected=expected, cwd=cwd)["receipt"]

    def edit_receipt(self, receipt, change):
        path = Path(receipt)
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    def test_pass_show_and_private_storage(self):
        receipt = self.capture()
        result = self.invoke("show", receipt, expected="PASS_CURRENT")
        self.assertEqual(result["evidence"]["returncode"], 0)
        self.assertEqual(Path(receipt).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(receipt).parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("Ignored files", result["scope_exclusions"][0])
        self.assertEqual(self.git("status", "--porcelain"), b"")

    def test_failed_command(self):
        receipt = self.capture("raise SystemExit(7)", expected="FAIL_CURRENT")
        self.assertEqual(self.invoke("show", receipt)["evidence"]["returncode"], 7)

    def test_tracked_change(self):
        receipt = self.capture()
        (self.root / "source.txt").write_text("changed\n")
        self.invoke("status", receipt, expected="STALE")

    def test_staged_change_even_when_worktree_restored(self):
        receipt = self.capture()
        path = self.root / "source.txt"
        path.write_text("staged\n")
        self.git("add", "source.txt")
        path.write_text("original\n")
        self.invoke("status", receipt, expected="STALE")

    def test_head_change_without_tree_change(self):
        receipt = self.capture()
        self.git("commit", "--allow-empty", "-m", "Advance history")
        self.invoke("status", receipt, expected="STALE")

    def test_untracked_content_and_deletion(self):
        path = self.root / "new file\tand\nnewline.txt"
        path.write_text("first")
        receipt = self.capture()
        path.write_text("second")
        self.invoke("status", receipt, expected="STALE")
        receipt = self.capture()
        path.unlink()
        self.invoke("status", receipt, expected="STALE")

    def test_tracked_deletion(self):
        receipt = self.capture()
        (self.root / "source.txt").unlink()
        self.invoke("status", receipt, expected="STALE")

    def test_executable_mode(self):
        receipt = self.capture()
        (self.root / "source.txt").chmod(0o755)
        self.invoke("status", receipt, expected="STALE")

    def test_symlink_target_instead_of_referent(self):
        external = Path(self.temporary.name) / "external"
        external.write_text("outside")
        link = self.root / "link"
        link.symlink_to(external)
        receipt = self.capture()
        external.write_text("outside changed")
        self.invoke("status", receipt, expected="PASS_CURRENT")
        link.unlink()
        link.symlink_to("source.txt")
        self.invoke("status", receipt, expected="STALE")

    def test_directory_symlink_is_never_followed(self):
        external = Path(self.temporary.name) / "external-dir"
        external.mkdir()
        (external / "outside.txt").write_text("private")
        (self.root / "directory-link").symlink_to(external, target_is_directory=True)
        receipt = self.capture()
        (external / "outside.txt").write_text("modified")
        self.invoke("status", receipt, expected="PASS_CURRENT")

    def test_tracked_parent_replaced_by_directory_symlink_is_unknown(self):
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "file").write_text("tracked")
        self.git("add", "folder/file")
        self.git("commit", "-m", "Nested source")
        receipt = self.capture()
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "file").write_text("must not read")
        shutil.rmtree(folder)
        folder.symlink_to(outside, target_is_directory=True)
        self.invoke("status", receipt, expected="UNKNOWN")

    def test_ignored_outputs_are_explicitly_excluded(self):
        receipt = self.capture("from pathlib import Path; Path('ignored').mkdir(); Path('ignored/output').write_text('ok')")
        (self.root / "ignored" / "output").write_text("later")
        self.invoke("status", receipt, expected="PASS_CURRENT")

    def test_capture_edit_is_invalid(self):
        receipt = self.capture("from pathlib import Path; Path('source.txt').write_text('edited')", expected="INVALID")
        self.invoke("status", receipt, expected="INVALID")

    def test_timeout_and_child_cleanup(self):
        receipt = self.capture("import time; time.sleep(10)", timeout="0.1", expected="FAIL_CURRENT")
        self.assertTrue(self.invoke("show", receipt)["evidence"]["timed_out"])

    def test_background_child_does_not_outlive_capture(self):
        marker = Path(self.temporary.name) / "should-not-exist"
        child = f"import time; from pathlib import Path; time.sleep(0.7); Path({str(marker)!r}).touch()"
        source = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
        receipt = self.capture(source)
        subprocess.run([sys.executable, "-c", "import time; time.sleep(1)"], check=True)
        self.assertFalse(marker.exists())
        self.invoke("status", receipt, expected="PASS_CURRENT")

    def test_corrupt_json_and_log(self):
        receipt = self.capture()
        Path(receipt).write_text("not json")
        self.invoke("status", receipt, expected="INVALID")
        receipt = self.capture()
        Path(receipt).with_suffix(".log").write_text("modified log")
        self.invoke("status", receipt, expected="INVALID")

    def test_log_traversal_rejected(self):
        receipt = self.capture()
        self.edit_receipt(receipt, lambda data: data.update(log="../config"))
        self.invoke("show", receipt, expected="INVALID")

    def test_receipt_symlink_rejected(self):
        receipt = Path(self.capture())
        target = receipt.with_name("copy")
        receipt.rename(target)
        receipt.symlink_to(target)
        self.invoke("status", str(receipt), expected="INVALID")

    def test_log_symlink_rejected(self):
        receipt = self.capture()
        log = Path(receipt).with_suffix(".log")
        log.unlink()
        log.symlink_to(self.root / "source.txt")
        self.invoke("status", receipt, expected="INVALID")

    def test_wrong_repository(self):
        receipt = self.capture()
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(other)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.invoke("status", receipt, cwd=other, expected="INVALID")
        store = other / ".git" / "verification-receipts"
        store.mkdir(mode=0o700)
        for path in (Path(receipt), Path(receipt).with_suffix(".log")):
            shutil.copy(path, store / path.name)
        self.invoke("status", str(store / Path(receipt).name), cwd=other, expected="INVALID")

    def test_hostile_command_data_is_never_replayed(self):
        receipt = self.capture()
        marker = self.root / "no-replay"
        malicious = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
        self.edit_receipt(receipt, lambda data: data.update(argv=malicious))
        self.invoke("show", receipt, expected="PASS_CURRENT")
        self.invoke("status", receipt, expected="PASS_CURRENT")
        self.assertFalse(marker.exists())

    def test_shell_metacharacters_remain_literal_arguments(self):
        argument = "$(touch bad); echo nope && `touch worse`"
        receipt = self.invoke("capture", "--", sys.executable, "-c", "import sys; print(sys.argv[1])", argument,
                              expected="PASS_CURRENT")["receipt"]
        self.assertEqual(Path(receipt).with_suffix(".log").read_text().strip(), argument)
        self.assertFalse((self.root / "bad").exists())
        self.assertFalse((self.root / "worse").exists())

    def test_working_subdirectory(self):
        folder = self.root / "sub dir"
        folder.mkdir()
        receipt = self.capture(cwd=folder)
        self.assertEqual(self.invoke("show", receipt)["evidence"]["cwd"], "sub dir")

    def test_fifo_fails_closed_without_running_command(self):
        os.mkfifo(self.root / "pipe")
        marker = Path(self.temporary.name) / "not-run"
        self.capture(f"from pathlib import Path; Path({str(marker)!r}).touch()", expected="UNKNOWN")
        self.assertFalse(marker.exists())

    def test_ignored_special_file_is_explicitly_outside_scope(self):
        ignored = self.root / "ignored"
        ignored.mkdir()
        os.mkfifo(ignored / "pipe")
        self.capture()

    def test_submodule_entry_fails_closed(self):
        commit = self.git("rev-parse", "HEAD").strip().decode()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{commit},vendor")
        self.capture(expected="UNKNOWN")

    def test_unborn_repository_supported(self):
        other = Path(self.temporary.name) / "unborn"
        other.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(other)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        receipt = self.capture(cwd=other)
        self.assertIsNone(self.invoke("show", receipt, cwd=other)["evidence"]["before"]["head"])

    def test_missing_executable_is_unknown(self):
        self.invoke("capture", "--", "receipt-nonexistent-command", expected="UNKNOWN")

    def test_unknown_schema_and_working_path_traversal(self):
        for change in ({"schema": 99}, {"cwd": "../outside"}, {"cwd": "/outside"}):
            with self.subTest(change=change):
                receipt = self.capture()
                self.edit_receipt(receipt, lambda data: data.update(change))
                self.invoke("status", receipt, expected="INVALID")

    def test_output_limit_returns_unknown(self):
        self.capture("import sys; sys.stdout.write('x' * (17 * 1024 * 1024))", expected="UNKNOWN")

    def test_large_source_file_is_unknown(self):
        with (self.root / "oversize").open("wb") as handle:
            handle.truncate(65 * 1024 * 1024)
        self.capture(expected="UNKNOWN")

    def test_linked_worktree_storage_and_repository_identity(self):
        worktree = Path(self.temporary.name) / "linked worktree"
        self.git("worktree", "add", "-b", "feature", str(worktree))
        receipt = self.capture(cwd=worktree)
        self.assertTrue(Path(receipt).is_relative_to(self.root / ".git" / "worktrees"))
        self.invoke("status", receipt, cwd=worktree, expected="PASS_CURRENT")
        self.invoke("status", receipt, expected="INVALID")

    def test_unmerged_index_is_unknown(self):
        object_id = self.git("rev-parse", "HEAD:source.txt").strip().decode()
        subprocess.run(["git", "-C", str(self.root), "update-index", "--index-info"],
                       input=f"0 {'0' * 40}\tsource.txt\n100644 {object_id} 1\tsource.txt\n".encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.capture(expected="UNKNOWN")


if __name__ == "__main__":
    unittest.main()
