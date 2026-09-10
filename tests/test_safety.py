from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


# 测试环境不需要启动 PyWebIO，只提供模块导入所需的最小占位符。
if "pywebio" not in sys.modules:
    pywebio = types.ModuleType("pywebio")
    pywebio.start_server = lambda *args, **kwargs: None
    pywebio_input = types.ModuleType("pywebio.input")
    pywebio_input.checkbox = lambda *args, **kwargs: []
    pywebio_input.input = lambda *args, **kwargs: ""
    pywebio_output = types.ModuleType("pywebio.output")
    for name in ("put_markdown", "put_table", "put_text", "popup", "put_html"):
        setattr(pywebio_output, name, lambda *args, **kwargs: None)
    pywebio_output.Output = object
    pywebio_output.use_scope = lambda *args, **kwargs: mock.MagicMock()
    sys.modules.update({
        "pywebio": pywebio,
        "pywebio.input": pywebio_input,
        "pywebio.output": pywebio_output,
    })

from android_backup import contacts, photos  # noqa: E402


def _write_tar(path: Path, member_name: str, *, symlink: bool = False) -> None:
    with tarfile.open(path, "w") as tf:
        info = tarfile.TarInfo(member_name)
        if symlink:
            info.type = tarfile.SYMTYPE
            info.linkname = "/data/local/tmp/escape"
            tf.addfile(info)
        else:
            payload = b"sqlite"
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))


class TarSafetyTests(unittest.TestCase):
    def test_accepts_expected_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "contacts.tar"
            _write_tar(archive, "databases/contacts2.db")
            contacts._validate_restore_tar(archive, "databases")

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.tar"
            _write_tar(archive, "../outside")
            with self.assertRaisesRegex(ValueError, "非法路径"):
                contacts._validate_restore_tar(archive, "databases")

    def test_rejects_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "link.tar"
            _write_tar(archive, "databases/link", symlink=True)
            with self.assertRaisesRegex(ValueError, "链接"):
                contacts._validate_restore_tar(archive, "databases")

    def test_extract_failure_rolls_back_original_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "contacts.tar"
            _write_tar(archive, "databases/contacts2.db")
            commands: list[str] = []

            def fake_shell(command: str, check: bool = True) -> str:
                commands.append(command)
                if "echo MOVED" in command:
                    return "MOVED\n"
                if "tar -xpf" in command:
                    raise contacts.adb.AdbError("broken tar")
                return ""

            with (mock.patch.object(contacts.adb, "push"),
                  mock.patch.object(contacts.adb, "shell", side_effect=fake_shell)):
                with self.assertRaises(contacts.adb.AdbError):
                    contacts._push_untar_remote(
                        archive, "/data/local/tmp/restore.tar",
                        contacts.PKG_ROOT, contacts.SUB_DIR,
                    )

            self.assertTrue(any(
                command.startswith("mv '") and ".bak_" in command
                and f"' '{contacts.PKG_ROOT}/{contacts.SUB_DIR}'" in command
                for command in commands
            ))

    def test_tar_command_failure_is_not_treated_as_missing_directory(self) -> None:
        completed = SimpleNamespace(returncode=1, stdout="", stderr="no space")
        resolved = SimpleNamespace(
            returncode=0,
            stdout=f"{contacts.PKG_ROOT}/{contacts.SUB_DIR}\n",
            stderr="",
        )
        with (mock.patch.object(contacts, "_remote_exists", return_value=True),
              mock.patch.object(contacts.adb, "run_adb",
                                side_effect=[resolved, completed])):
            with self.assertRaisesRegex(RuntimeError, "no space"):
                contacts._tar_remote_dir(
                    contacts.PKG_ROOT, contacts.SUB_DIR,
                    "/data/local/tmp/contacts.tar",
                )


class PhotoSafetyTests(unittest.TestCase):
    def test_album_paths_are_posix_on_windows(self) -> None:
        with (mock.patch.object(
                photos, "_list_dirs_maxdepth",
                return_value=["/sdcard/DCIM/Camera"]),
              mock.patch.object(photos, "_dir_has_media_files", return_value=True)):
            albums = photos._scan_album_dirs("/sdcard")
        self.assertEqual(albums[0]["rel_path"], "DCIM/Camera")

    def test_rejects_unsafe_manifest_paths(self) -> None:
        for value in ("../DCIM", "/sdcard/DCIM", "DCIM\\Camera", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    photos._validated_rel_path(value)

    def test_empty_album_selection_is_reported_as_skipped(self) -> None:
        with (mock.patch.object(photos, "_resolve_root", return_value="/sdcard"),
              mock.patch.object(photos, "_scan_album_dirs", return_value=[]),
              mock.patch.object(photos.ui, "progress_start"),
              mock.patch.object(photos.ui, "progress_done")):
            self.assertFalse(photos.backup(Path("unused")))

    def test_restore_raises_when_an_album_push_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            album = root / "photos" / "DCIM" / "Camera"
            album.mkdir(parents=True)
            (album / "one.jpg").write_bytes(b"jpg")
            (root / "photos" / "manifest.json").write_text(json.dumps({
                "albums": [{
                    "local_relpath": "DCIM/Camera",
                    "file_count": 1,
                    "size_bytes": 3,
                }]
            }), encoding="utf-8")

            with (mock.patch.object(photos, "_resolve_root", return_value="/sdcard"),
                  mock.patch.object(photos.ui, "select_items",
                                    return_value=["DCIM/Camera"]),
                  mock.patch.object(photos.ui, "progress_start"),
                  mock.patch.object(photos.ui, "progress_done"),
                  mock.patch.object(photos.adb, "shell", return_value=""),
                  mock.patch.object(
                      photos.adb, "run_adb",
                      return_value=SimpleNamespace(returncode=1, stderr="push failed"))):
                with self.assertRaisesRegex(RuntimeError, "部分失败"):
                    photos.restore(root)


if __name__ == "__main__":
    unittest.main()
