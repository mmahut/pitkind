import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from pitkind import io_paths


def test_filenames_distinguish_runs_within_the_same_second():
    started_at = datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert io_paths.output_filename("proposal", started_at) != io_paths.output_filename(
        "proposal", started_at + timedelta(microseconds=1)
    )


def test_failed_write_preserves_destination_and_removes_temporary_file(tmp_path, monkeypatch):
    destination = tmp_path / "result.json"
    io_paths.atomic_write_json(destination, {"original": True})
    original = destination.read_bytes()

    def fail_dump(data, stream, **kwargs):
        stream.write('{"incomplete":')
        raise OSError("disk full")

    monkeypatch.setattr(io_paths.json, "dump", fail_dump)
    with pytest.raises(OSError, match="disk full"):
        io_paths.atomic_write_json(destination, {"replacement": True})
    assert destination.read_bytes() == original
    assert list(tmp_path.iterdir()) == [destination]


def test_overlapping_writers_use_distinct_temporary_files(tmp_path, monkeypatch):
    destination = tmp_path / "result.json"
    ready = Barrier(2, timeout=5)
    replace = io_paths.os.replace
    sources = []

    def overlapping_replace(source, target):
        sources.append(Path(source))
        ready.wait()
        replace(source, target)

    monkeypatch.setattr(io_paths.os, "replace", overlapping_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(io_paths.atomic_write_json, destination, {"writer": i}) for i in range(2)]
        for future in futures:
            future.result()
    assert len(set(sources)) == 2
    assert all(source.parent == tmp_path for source in sources)
    assert json.loads(destination.read_text()) in [{"writer": 0}, {"writer": 1}]
    assert list(tmp_path.iterdir()) == [destination]
