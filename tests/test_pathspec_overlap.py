from mcp_agent_mail.app import _compile_pathspec, _patterns_overlap


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


def test_pathspec_cache_hit_ratio_after_warmup() -> None:
    _compile_pathspec.cache_clear()
    for _ in range(20):
        assert _patterns_overlap("src/**", "src/file.txt")
        assert _patterns_overlap("docs/**", "docs/readme.md")
        assert _patterns_overlap("assets/*.png", "assets/logo.png")
    info = _compile_pathspec.cache_info()
    total = info.hits + info.misses
    assert total > 0
    ratio = info.hits / total
    assert ratio >= 0.9, f"cache hit ratio too low: {ratio:.2%}"
