import tempfile
from pathlib import Path
import unittest
from bot.media_intake import owned_intake


class MediaIntakeTests(unittest.TestCase):
    def test_only_explicit_generated_temp_copy_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'temp';root.mkdir()
            owned=root/'vclone_ref_12345678.wav';owned.write_bytes(b'copy')
            user=root/'my_sample.wav';user.write_bytes(b'original')
            outside=Path(directory)/'vclone_ref_12345678.wav';outside.write_bytes(b'original')
            cap=owned_intake(owned,user,outside,root=root);cap.cleanup()
            self.assertFalse(owned.exists());self.assertTrue(user.exists());self.assertTrue(outside.exists())
    def test_replaced_file_and_symlink_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);file=root/'dub_input_1_2_abcdef.mp4';file.write_bytes(b'copy')
            cap=owned_intake(file,root=root);file.unlink();file.write_bytes(b'replacement')
            cap.cleanup();self.assertTrue(file.exists())
            target=root/'personal.wav';target.write_bytes(b'private')
            link=root/'vclone_ref_12345678.wav';link.symlink_to(target)
            owned_intake(link,root=root).cleanup()
            self.assertTrue(link.is_symlink());self.assertEqual(target.read_bytes(),b'private')
