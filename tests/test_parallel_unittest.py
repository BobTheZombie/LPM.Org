import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from lpm import app, parallel


class ParallelTests(unittest.TestCase):
    def test_affinity_and_limits(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(os, 'sched_getaffinity', return_value={0, 2}), patch.object(Path, 'read_text', return_value='max 100000'):
            self.assertEqual(parallel.worker_count('build'), 2)
            self.assertEqual(parallel.worker_count(), 2)
        with patch.dict(os.environ, LPM_IO_WORKERS='0'):
            with self.assertRaises(ValueError):
                parallel.worker_count()

    def test_container_cpu_quota(self):
        with patch.object(os, 'sched_getaffinity', return_value=set(range(16))), \
             patch.object(Path, 'read_text', return_value='200000 100000'):
            self.assertEqual(parallel.cpu_count(), 2)

    def test_database_retains_durability_and_bounded_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = app._open_state_db(Path(tmp)/'state.db')
            try:
                self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
                self.assertEqual(conn.execute('PRAGMA cache_size').fetchone()[0], -8192)
                self.assertEqual(conn.execute('PRAGMA synchronous').fetchone()[0], 2)
            finally:
                conn.close()

    def test_ordered_map_preserves_results_and_errors(self):
        with patch.dict(os.environ, LPM_IO_WORKERS='4'):
            self.assertEqual(list(parallel.ordered_map(lambda x: x*x, range(100))), [x*x for x in range(100)])
            with self.assertRaises(ZeroDivisionError):
                list(parallel.ordered_map(lambda x: 1/x, [1, 0, 2]))

    def test_build_environment_respects_user_settings(self):
        with patch.dict(os.environ, LPM_BUILD_JOBS='3'):
            env = parallel.configure_build_environment({})
            self.assertEqual(env['MAKEFLAGS'], '-j3')
            self.assertEqual(env['CMAKE_BUILD_PARALLEL_LEVEL'], '3')
            env = parallel.configure_build_environment({'MAKEFLAGS':'-j1', 'CMAKE_BUILD_PARALLEL_LEVEL':'1'})
            self.assertEqual(env['MAKEFLAGS'], '-j1')
            self.assertEqual(env['CMAKE_BUILD_PARALLEL_LEVEL'], '1')

    def test_manifest_identical_serial_parallel_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(40):
                (root / str(i)).write_bytes(bytes([i]) * 4096)
            (root/'link').symlink_to('1')
            with patch.dict(os.environ, LPM_IO_WORKERS='1'):
                serial = app.collect_manifest(root)
            with patch.dict(os.environ, LPM_IO_WORKERS='4'):
                self.assertEqual(app.collect_manifest(root), serial)
            self.assertEqual(app.sha256sum(root/'2'), hashlib.sha256((root/'2').read_bytes()).hexdigest())

    def test_stream_extract_preserves_payload_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode='w') as archive:
                data = b'a' * (2 << 20)
                member = tarfile.TarInfo('usr/share/data')
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
                link = tarfile.TarInfo('usr/share/link')
                link.type = tarfile.SYMTYPE
                link.linkname = 'data'
                archive.addfile(link)
            blob = root/'payload.zst'
            blob.write_bytes(app.zstd.ZstdCompressor().compress(raw.getvalue()))
            app.extract_tar(blob, root/'dest')
            self.assertEqual((root/'dest/usr/share/data').read_bytes(), data)
            self.assertEqual(os.readlink(root/'dest/usr/share/link'), 'data')


if __name__ == '__main__':
    unittest.main()
