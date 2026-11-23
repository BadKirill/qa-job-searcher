from types import SimpleNamespace

import pytest

from qa_job_bot.web import stage_task_directory
from qa_job_bot.web_store import StoreError


@pytest.mark.parametrize(
    "task_id", ["", ".", "..", "../outside", "/outside", "nested/child", "nested\\child"]
)
def test_deletion_rejects_path_components_before_filesystem_access(tmp_path, task_id):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("private file")
    settings = SimpleNamespace(data_dir=tmp_path / "data")
    with pytest.raises(StoreError, match="небезопасен"):
        stage_task_directory(settings, task_id)
    assert marker.read_text() == "private file"
    assert not settings.data_dir.exists()
