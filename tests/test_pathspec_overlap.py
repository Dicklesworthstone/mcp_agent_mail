from mcp_agent_mail.app import _patterns_overlap  # type: ignore


def test_overlap_basic_globs() -> None:
    assert _patterns_overlap("src/**", "src/file.txt")
    assert _patterns_overlap("src/**", "src/dir/nested.py")
    assert not _patterns_overlap("docs/**", "src/**")


def test_overlap_exact_files() -> None:
    assert _patterns_overlap("README.md", "README.md")
    assert not _patterns_overlap("README.md", "LICENSE")


def test_overlap_cross_match() -> None:
    # cross-match heuristic should detect that pattern and path overlap
    assert _patterns_overlap("assets/*.png", "assets/logo.png")
    assert not _patterns_overlap("assets/*.png", "assets/logo.jpg")


