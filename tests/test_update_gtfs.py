import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import zipfile

from scripts import update_gtfs as updater


def make_feed(path, version="one", extra=None, compression=zipfile.ZIP_STORED):
    files = {
        "agency.txt": "agency_name,agency_url,agency_timezone\nAT,https://at.govt.nz,Pacific/Auckland\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n1,Test,-36,174\n",
        "routes.txt": "route_id,route_type\n1,3\n",
        "trips.txt": "route_id,service_id,trip_id\n1,1,1\n",
        "stop_times.txt": "trip_id,stop_id,stop_sequence\n1,1,1\n",
        "calendar_dates.txt": "service_id,date,exception_type\n1,20990101,1\n",
        "feed_info.txt": "feed_publisher_name,feed_version,feed_start_date,feed_end_date\n"
                         f"AT,{version},20200101,20991231\n",
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w", compression=compression) as bundle:
        for name, contents in files.items():
            bundle.writestr(name, contents)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.current = self.root / "gtfs.zip"
        make_feed(self.current)
        with zipfile.ZipFile(self.current) as bundle:
            bundle.extractall(self.root / "gtfs")
        self.incoming = self.root / "incoming.zip"
        make_feed(self.incoming, "two")
        self.download = patch.object(updater, "download", side_effect=lambda dest: shutil.copyfile(self.incoming, dest))
        self.download.start()
        self.addCleanup(self.download.stop)
        self.environment = patch.dict(updater.os.environ, {"GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_repackaging_is_unchanged(self):
        make_feed(self.incoming, compression=zipfile.ZIP_DEFLATED)
        original = self.current.read_bytes()
        self.assertFalse(updater.update(self.root))
        self.assertEqual(self.current.read_bytes(), original)

    def test_update_archives_both_before_replacing_and_removes_obsolete_files(self):
        (self.root / "gtfs" / "obsolete.txt").write_text("old")
        original = self.current.read_bytes()
        with patch.object(updater, "GitHub") as github:
            def check_archive(path, info, **kwargs):
                self.assertEqual(self.current.read_bytes(), original)
            github.return_value.archive.side_effect = check_archive
            self.assertTrue(updater.update(self.root, archive=True))
            self.assertEqual(github.return_value.archive.call_count, 2)
        self.assertEqual(self.current.read_bytes(), self.incoming.read_bytes())
        self.assertFalse((self.root / "gtfs" / "obsolete.txt").exists())

    def test_dry_run_has_no_changes_or_releases(self):
        original = self.current.read_bytes()
        with patch.object(updater, "GitHub") as github:
            self.assertTrue(updater.update(self.root, dry_run=True, archive=True))
            github.assert_not_called()
        self.assertEqual(self.current.read_bytes(), original)

    def test_failed_archive_preserves_existing_data(self):
        original = self.current.read_bytes()
        with patch.object(updater, "GitHub") as github:
            github.return_value.archive.side_effect = RuntimeError("upload failed")
            with self.assertRaises(RuntimeError):
                updater.update(self.root, archive=True)
        self.assertEqual(self.current.read_bytes(), original)
        self.assertEqual(updater.inspect_feed(self.current)["feed_version"], "one")
        self.assertIn("one", (self.root / "gtfs" / "feed_info.txt").read_text())

    def test_invalid_download_preserves_data_and_does_not_archive(self):
        self.incoming.write_text("upstream error page")
        original = self.current.read_bytes()
        with patch.object(updater, "GitHub") as github:
            with self.assertRaises(zipfile.BadZipFile):
                updater.update(self.root, archive=True)
            github.assert_not_called()
        self.assertEqual(self.current.read_bytes(), original)

    def test_network_failure_preserves_data(self):
        original = self.current.read_bytes()
        with patch.object(updater, "download", side_effect=URLError("connection failed")):
            with self.assertRaises(URLError):
                updater.update(self.root)
        self.assertEqual(self.current.read_bytes(), original)

    def test_unchanged_feed_still_archives_initial_snapshot(self):
        shutil.copyfile(self.current, self.incoming)
        with patch.object(updater, "GitHub") as github:
            self.assertFalse(updater.update(self.root, archive=True))
            github.return_value.archive.assert_called_once()

    def test_repair_extracted_data(self):
        shutil.copyfile(self.current, self.incoming)
        (self.root / "gtfs" / "stops.txt").unlink()
        self.assertTrue(updater.update(self.root))
        self.assertTrue((self.root / "gtfs" / "stops.txt").exists())

    def test_validation_rejects_unsafe_entries_bad_rows_and_expired_dates(self):
        cases = [
            {"../escape.txt": "bad"},
            {"stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n1,missing\n"},
            {"routes.txt": "bad_header\n1\n"},
            {"feed_info.txt": "feed_publisher_name,feed_version,feed_start_date,feed_end_date\nAT,old,20200101,20200102\n"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                make_feed(self.incoming, extra=extra)
                with self.assertRaises(ValueError):
                    updater.inspect_feed(self.incoming)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "gtfs.zip"
        make_feed(self.path)
        self.info = updater.inspect_feed(self.path)
        environment = patch.dict(updater.os.environ, {
            "GH_TOKEN": "test-token", "GITHUB_REPOSITORY": "test/repo", "GITHUB_SHA": "abc123"})
        environment.start()
        self.addCleanup(environment.stop)
        self.github = updater.GitHub()
        self.release = {"id": 1, "tag_name": "gtfs-" + self.info["content_sha256"],
                        "assets": [], "draft": True, "url": self.github.base + "/1",
                        "upload_url": "https://uploads.github.com/test{?name,label}"}

    def missing(self):
        error = HTTPError("url", 404, "Not Found", {}, None)
        self.addCleanup(error.close)
        return error

    def respond(self, responses):
        responses = iter(responses)

        def request(url, method="GET", data=None, content_type=None):
            if url.startswith("https://uploads.github.com"):
                return {"state": "uploaded", "size": len(data)}
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result
        return request

    def test_new_release_publishes_only_after_both_uploads(self):
        with patch.object(self.github, "request", side_effect=self.respond([self.missing(), [], self.release, {}])) as request:
            self.github.archive(self.path, self.info)
        calls = request.call_args_list
        self.assertTrue(calls[2].args[2]["draft"])
        self.assertIn("name=gtfs.zip", calls[3].args[0])
        metadata = json.loads(calls[4].args[2])
        self.assertEqual(metadata["zip_sha256"], self.info["zip_sha256"])
        self.assertEqual(calls[5].args[1:], ("PATCH", {"draft": False, "make_latest": "false"}))

    def test_existing_published_release_is_untouched(self):
        release = copy.deepcopy(self.release)
        release.update(draft=False, assets=[{"name": name, "state": "uploaded", "size": 100}
                                            for name in ("gtfs.zip", "metadata.json")])
        with patch.object(self.github, "request", return_value=release) as request:
            self.github.archive(self.path, self.info)
        self.assertEqual(request.call_count, 1)

    def test_interrupted_draft_resumes_missing_asset(self):
        release = copy.deepcopy(self.release)
        release["assets"] = [{"name": "gtfs.zip", "state": "uploaded", "size": 100}]
        with patch.object(self.github, "request", side_effect=self.respond([self.missing(), [release], {}])) as request:
            self.github.archive(self.path, self.info)
        self.assertIn("name=metadata.json", request.call_args_list[2].args[0])
        self.assertEqual(request.call_args_list[3].args[1], "PATCH")

    def test_permission_errors_do_not_create_releases(self):
        denied = HTTPError("url", 403, "Forbidden", {}, None)
        self.addCleanup(denied.close)
        with patch.object(self.github, "request", side_effect=denied) as request:
            with self.assertRaises(HTTPError):
                self.github.archive(self.path, self.info)
        self.assertEqual(request.call_count, 1)

    def test_incomplete_upload_is_not_published(self):
        with patch.object(self.github, "request", side_effect=[self.release, {"state": "starter", "size": 0}]) as request:
            with self.assertRaises(RuntimeError):
                self.github.archive(self.path, self.info)
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
