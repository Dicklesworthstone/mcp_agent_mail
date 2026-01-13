# ADR-002: Rust/PyO3 Optimization Analysis

## Status

**Decided** - Not pursuing Rust optimization at this time (January 2026)

## Context

During performance optimization work on the file reservation system, we investigated whether rewriting hot paths in Rust (via PyO3/maturin) would significantly improve performance.

The file reservation system performs pattern matching to detect conflicts between agents editing overlapping file paths. This is a critical path executed on every `file_reservation_paths` call and in the pre-commit/pre-push guard hooks.

## Decision

**Do not pursue Rust/PyO3 optimization.** The system is I/O bound, not CPU bound. Python-side optimizations (LRU caching, Union PathSpec) already provide sufficient performance.

## Analysis

### Request Latency Breakdown

For a typical `file_reservation_paths` request (50 paths x 100 reservations):

| Component | Time | % of Total | Rust Would Help? |
|-----------|------|------------|------------------|
| Database queries | 7.5ms | 17% | No (I/O bound) |
| Git operations | 35ms | 79% | No (I/O bound) |
| Pattern matching (optimized) | 1.09ms | 2% | Yes, but negligible impact |
| JSON serialization | 0.02ms | <0.1% | Marginally |

**Key insight**: Pattern matching is only 2% of total request time. Even a 100x speedup on pattern matching would only improve end-to-end latency by ~2%.

### Pattern Matching Performance Evolution

| Approach | Time | Speedup vs Baseline |
|----------|------|---------------------|
| Baseline (uncached Python) | 28.89ms | 1x |
| LRU-cached PathSpec | 2.77ms | 10x |
| Union PathSpec (current) | 1.09ms | 26x |
| **Rust globset (estimated)** | 0.05ms | ~580x |

The Python optimizations (caching + Union PathSpec) already achieve 26x speedup. Rust would provide an additional ~22x on pattern matching alone, but this translates to only **~1.02x** improvement on end-to-end request latency.

### Cost-Benefit Analysis

#### Python Optimization (Implemented)

- **Effort**: 2-4 hours
- **Risk**: Very low (pure refactor)
- **Pattern matching speedup**: 26x
- **End-to-end improvement**: 1.6x
- **Maintenance**: None (standard Python)

#### Rust/PyO3 Optimization (Not Pursued)

- **Effort**: 1-2 days implementation + ongoing maintenance
- **Risk**: Medium (build complexity, cross-platform issues, CI changes)
- **Additional pattern matching speedup**: ~22x beyond Python
- **End-to-end improvement**: ~1.02x beyond Python-optimized
- **Maintenance**: Rust toolchain, maturin builds, wheel distribution, platform-specific debugging

### When Rust WOULD Make Sense

Revisit this decision if any of these conditions change:

1. **Scale**: >10,000 patterns and >1,000 paths per request
2. **Throughput**: >10,000 requests/second where CPU becomes bottleneck
3. **Pattern matching becomes >20% of request time**
4. **Standalone binary requirement**: Pre-commit guard as native executable to avoid Python startup latency
5. **CPU-intensive operations added**: Image processing, cryptography, compression

### Recommended Rust Crates (Future Reference)

If requirements change and Rust optimization becomes worthwhile:

| Crate | Purpose | Notes |
|-------|---------|-------|
| **globset** | Multi-pattern glob matching | 10-100x faster than Python pathspec |
| **regex** | Regular expressions | SIMD acceleration |
| **serde_json** | JSON serialization | Fast serialization |
| **dashmap** | Concurrent hashmap | For multi-threaded scenarios |
| **pyo3** | Python bindings | For PyO3 extension module |
| **maturin** | Build tool | For packaging Rust+Python |

### PyO3/Maturin Setup (Reference)

If Rust extension is ever needed, here's the setup:

```bash
# Initialize Rust extension in project
pip install maturin
cd src/mcp_agent_mail
maturin init --bindings pyo3

# Build wheel
maturin build --release

# Project structure would be:
src/
  mcp_agent_mail/
    _rust/
      Cargo.toml
      src/lib.rs     # PyO3 bindings
```

Example PyO3 binding for pattern matching:

```rust
use pyo3::prelude::*;
use globset::{Glob, GlobSetBuilder};

#[pyfunction]
fn match_patterns(patterns: Vec<String>, paths: Vec<String>) -> PyResult<Vec<bool>> {
    let mut builder = GlobSetBuilder::new();
    for pattern in &patterns {
        builder.add(Glob::new(pattern).map_err(|e| {
            PyErr::new::<pyo3::exceptions::PyValueError, _>(e.to_string())
        })?);
    }
    let set = builder.build().map_err(|e| {
        PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string())
    })?;

    Ok(paths.iter().map(|p| set.is_match(p)).collect())
}

#[pymodule]
fn _rust(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(match_patterns, m)?)?;
    Ok(())
}
```

## Consequences

### Positive

- Avoided unnecessary complexity in build system
- No additional maintenance burden (Rust toolchain, cross-platform builds)
- Python-only codebase remains accessible to more contributors
- Focus on actual bottlenecks (I/O operations) rather than premature optimization

### Negative

- Pattern matching is not as fast as theoretically possible
- If scale increases dramatically, will need to revisit

### Neutral

- This ADR documents the analysis for future reference
- Future developers can quickly understand why Rust wasn't pursued
- Clear criteria established for when to reconsider

## Related Work

- **mcp_agent_mail-3sd**: PathSpec LRU cache implementation (10x speedup)
- **mcp_agent_mail-dhn**: Union PathSpec for bulk conflict detection (26x speedup)
- **mcp_agent_mail-wjm**: Performance benchmark tests to validate optimizations

## References

- [PyO3 User Guide](https://pyo3.rs/)
- [Maturin Documentation](https://www.maturin.rs/)
- [Rust globset crate](https://docs.rs/globset/latest/globset/)
- [Python pathspec library](https://github.com/cpburnz/python-pathspec)
- [Git wildmatch specification](https://git-scm.com/docs/gitignore)

## Decision Date

January 2026

## Decision Makers

Performance analysis conducted during pattern matching optimization work.
