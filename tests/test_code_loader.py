from app.services.code_loader import load_repository_source_context


def test_source_snapshot_prioritizes_referenced_files_and_skips_sensitive_content(tmp_path):
    (tmp_path / "a.py").write_text("A = 'first'\n", encoding="utf-8")
    (tmp_path / "important.py").write_text("VALUE = 'preferred'\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=do-not-send\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_important.py").write_text("assert False\n", encoding="utf-8")

    context = load_repository_source_context(
        str(tmp_path),
        "python",
        45,
        {"important.py"},
    )

    assert context.files == ("important.py",)
    assert "preferred" in context.content
    assert "do-not-send" not in context.content
    assert "test_important" not in context.content
    assert context.truncated is True
