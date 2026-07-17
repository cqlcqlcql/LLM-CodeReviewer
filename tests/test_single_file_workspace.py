from pathlib import Path

from app.services.single_file_workspace import WORKSPACE_ROOT, single_file_repository


def test_review_workspace_is_outside_application_reload_tree():
    application_root = Path(__file__).resolve().parents[1]

    assert not WORKSPACE_ROOT.resolve().is_relative_to(application_root)

    with single_file_repository("example.py", b"value = 1\n") as repository:
        assert repository.parent == WORKSPACE_ROOT.resolve()
        assert (repository / ".git").is_dir()
        assert (repository / "example.py").read_bytes() == b"value = 1\n"
