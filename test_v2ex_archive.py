import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from job_sources.v2ex.archive import V2exArchiveJobSource, main
from job_sources.v2ex.source import V2exJobSource
from job_sources.v2ex.viewer import V2exViewerApp
from test_v2ex_source import (
    FakeResponse,
    RecordingOpener,
    SequenceOpener,
    call_viewer,
    feed,
    item,
)


class V2exArchiveJobSourceTests(unittest.TestCase):
    def test_repeated_snapshot_is_deduplicated_and_advances_last_seen(self):
        clock = iter(
            (
                datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc),
            )
        )
        opener = RecordingOpener(
            FakeResponse(
                feed(
                    item(
                        item_id="https://www.v2ex.com/t/9001",
                        title="[招聘] Python 工程师",
                    )
                )
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            source = V2exArchiveJobSource(
                database_path=Path(directory) / "jobs.db",
                feed_source=V2exJobSource(opener=opener),
                now=lambda: next(clock),
            )

            first = source.fetch()
            second = source.fetch()

        self.assertEqual(first.status, "complete")
        self.assertEqual(first.new_count, 1)
        self.assertEqual(first.updated_count, 0)
        self.assertEqual(len(first.outcomes), 1)
        self.assertEqual(
            first.outcomes[0].first_seen_at,
            "2026-08-04T01:00:00+00:00",
        )
        self.assertEqual(first.outcomes[0].last_seen_at, first.outcomes[0].first_seen_at)
        self.assertEqual(second.new_count, 0)
        self.assertEqual(second.updated_count, 1)
        self.assertEqual(len(second.outcomes), 1)
        self.assertEqual(
            second.outcomes[0].first_seen_at,
            "2026-08-04T01:00:00+00:00",
        )
        self.assertEqual(
            second.outcomes[0].last_seen_at,
            "2026-08-05T01:00:00+00:00",
        )

    def test_failed_feed_returns_stale_history_without_touching_last_seen(self):
        clock = iter(
            (
                datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc),
            )
        )
        opener = SequenceOpener(
            FakeResponse(feed(item(item_id="https://www.v2ex.com/t/9002"))),
            FakeResponse(b"rate limited", status=429),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = V2exArchiveJobSource(
                database_path=Path(directory) / "jobs.db",
                feed_source=V2exJobSource(opener=opener),
                now=lambda: next(clock),
            )
            source.fetch()

            stale = source.fetch()

        self.assertEqual(stale.status, "failed")
        self.assertEqual(stale.error_code, "rate_limited")
        self.assertTrue(stale.stale)
        self.assertEqual(len(stale.outcomes), 1)
        self.assertEqual(
            stale.outcomes[0].last_seen_at,
            "2026-08-04T01:00:00+00:00",
        )
        self.assertEqual(stale.new_count, 0)
        self.assertEqual(stale.updated_count, 0)

    def test_same_canonical_topic_url_is_archived_once(self):
        canonical_url = "https://www.v2ex.com/t/9003"
        first_item = item(item_id="feed-entry-a", title="[招聘] Python 工程师")
        first_item["url"] = canonical_url
        second_item = item(item_id="feed-entry-b", title="[招聘] Python 工程师（更新）")
        second_item["url"] = canonical_url
        with tempfile.TemporaryDirectory() as directory:
            source = V2exArchiveJobSource(
                database_path=Path(directory) / "jobs.db",
                feed_source=V2exJobSource(
                    opener=RecordingOpener(
                        FakeResponse(feed(first_item, second_item))
                    )
                ),
                now=lambda: datetime(
                    2026,
                    8,
                    4,
                    1,
                    0,
                    tzinfo=timezone.utc,
                ),
            )

            result = source.fetch()

        self.assertEqual(len(result.outcomes), 1)
        self.assertEqual(result.new_count, 1)
        self.assertEqual(result.updated_count, 0)
        self.assertEqual(result.outcomes[0].source_url, canonical_url)
        self.assertIn("更新", result.outcomes[0].safe_text)

    def test_unavailable_database_returns_a_safe_storage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = V2exArchiveJobSource(
                database_path=Path(directory),
                feed_source=V2exJobSource(
                    opener=RecordingOpener(FakeResponse(feed(item())))
                ),
                now=lambda: datetime(
                    2026,
                    8,
                    4,
                    1,
                    0,
                    tzinfo=timezone.utc,
                ),
            )

            result = source.fetch()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "local_storage_error")
        self.assertEqual(result.outcomes, ())

    def test_later_snapshot_updates_adds_and_never_deletes_absent_topics(self):
        first_topic = item(item_id="https://www.v2ex.com/t/9004")
        absent_topic = item(item_id="https://www.v2ex.com/t/9005")
        updated_topic = item(
            item_id="https://www.v2ex.com/t/9004",
            title="[招聘] Python 工程师（内容已更新）",
        )
        updated_topic["date_published"] = "not-a-date"
        new_topic = item(item_id="https://www.v2ex.com/t/9006")
        clock = iter(
            (
                datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            source = V2exArchiveJobSource(
                database_path=Path(directory) / "jobs.db",
                feed_source=V2exJobSource(
                    opener=SequenceOpener(
                        FakeResponse(feed(first_topic, absent_topic)),
                        FakeResponse(feed(updated_topic, new_topic)),
                    )
                ),
                now=lambda: next(clock),
            )
            source.fetch()

            result = source.fetch()

        by_url = {outcome.source_url: outcome for outcome in result.outcomes}
        self.assertEqual(len(by_url), 3)
        self.assertEqual(result.new_count, 1)
        self.assertEqual(result.updated_count, 1)
        self.assertIn("内容已更新", by_url["https://www.v2ex.com/t/9004"].safe_text)
        self.assertEqual(
            by_url["https://www.v2ex.com/t/9004"].published_at,
            "2026-07-30T09:00:00+00:00",
        )
        self.assertEqual(
            by_url["https://www.v2ex.com/t/9005"].last_seen_at,
            "2026-08-04T01:00:00+00:00",
        )


class V2exArchiveCommandTests(unittest.TestCase):
    def test_command_uses_environment_database_and_prints_safe_counts(self):
        output: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.db"

            exit_code = main(
                [],
                environ={"V2EX_ARCHIVE_DB_PATH": str(database_path)},
                feed_source=V2exJobSource(
                    opener=RecordingOpener(FakeResponse(feed(item())))
                ),
                now=lambda: datetime(
                    2026,
                    8,
                    4,
                    1,
                    0,
                    tzinfo=timezone.utc,
                ),
                output=output.append,
            )

            self.assertTrue(database_path.is_file())

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output,
            [
                "status=complete discovered=1 archived=1 "
                "new=1 updated=0 skipped=0 duplicates=0"
            ],
        )

    def test_windows_task_installer_dry_run_describes_safe_schedule(self):
        script = (
            Path(__file__).parent
            / "scripts"
            / "v2ex"
            / "install_daily_task.ps1"
        )

        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-PythonPath",
                sys.executable,
                "-At",
                "09:00",
                "-DryRun",
            ],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("schedule=Daily 09:00", completed.stdout)
        self.assertIn("start_when_available=True", completed.stdout)
        self.assertIn("multiple_instances=IgnoreNew", completed.stdout)
        self.assertIn(f"python={Path(sys.executable).resolve()}", completed.stdout)

    def test_windows_runner_dry_run_uses_explicit_interpreter_and_module(self):
        script = Path(__file__).parent / "scripts" / "v2ex" / "run_archive.ps1"

        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-PythonPath",
                sys.executable,
                "-DryRun",
            ],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f"python={Path(sys.executable).resolve()}", completed.stdout)
        self.assertIn("module=job_sources.v2ex.archive", completed.stdout)
        self.assertIn(
            f"repository={Path(__file__).parent.resolve()}",
            completed.stdout,
        )


class V2exArchiveViewerTests(unittest.TestCase):
    def test_cold_viewer_shows_stale_archive_when_feed_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.db"
            V2exArchiveJobSource(
                database_path=database_path,
                feed_source=V2exJobSource(
                    opener=RecordingOpener(
                        FakeResponse(
                            feed(
                                item(
                                    item_id="https://www.v2ex.com/t/9101",
                                    title="[招聘] 已归档 Python 工程师",
                                )
                            )
                        )
                    )
                ),
                now=lambda: datetime(
                    2026,
                    8,
                    4,
                    1,
                    0,
                    tzinfo=timezone.utc,
                ),
            ).fetch()
            app = V2exViewerApp(
                source=V2exArchiveJobSource(
                    database_path=database_path,
                    feed_source=V2exJobSource(
                        opener=RecordingOpener(
                            FakeResponse(b"rate limited", status=429)
                        )
                    ),
                    now=lambda: datetime(
                        2026,
                        8,
                        5,
                        1,
                        0,
                        tzinfo=timezone.utc,
                    ),
                ),
                now=lambda: 100.0,
            )

            status, _, body = call_viewer(app)

        page = body.decode("utf-8")
        self.assertEqual(status, "200 OK")
        self.assertIn("https://www.v2ex.com/t/9101", page)
        self.assertIn("刷新失败，正在显示本机归档历史", page)
        self.assertNotIn("当前没有可显示的缓存快照", page)


if __name__ == "__main__":
    unittest.main()
