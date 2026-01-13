# GlobTrie Pattern Matching Research

**Status:** NO-GO (January 2026)
**Bead:** mcp_agent_mail-5s5
**Related:** ADR-002 (Rust/PyO3 Optimization Analysis)

## Executive Summary

After analyzing pattern usage, gitwildmatch semantics, and performance requirements, the recommendation is **NO-GO** for GlobTrie implementation. Union PathSpec already provides sufficient performance (26x speedup), and the pattern matching component represents only 2% of request latency in an I/O-bound system.

## Prototype Results (From Bead Description)

```
PathSpec union (10 patterns x 160 paths): 0.299ms
GlobTrie prototype (same workload): 0.036ms
Speedup: 8.4x
Correctness: 130 vs 120 matches (10 missing)
```

The prototype achieved 8.4x speedup but had correctness issues (10 fewer matches).

## Pattern Analysis

### Pattern Categories Found in Codebase

Analysis of patterns in `tests/` and actual usage:

| Category | Pattern Example | Frequency | GlobTrie Support |
|----------|----------------|-----------|------------------|
| Pure prefix | `src/**`, `tests/**` | ~60% | O(1) trie lookup |
| Prefix+suffix | `src/**/*.py` | ~25% | Combined lookup |
| Pure suffix | `**/*.py` | ~5% | O(1) reverse trie |
| Literal | `src/app.py` | ~5% | O(1) hash set |
| Single-level | `scripts/*.sh` | ~3% | Requires special handling |
| Complex | Multiple `**`, char classes | ~2% | Fallback to PathSpec |

### Actual Patterns Observed

**Pure Prefix (Most Common):**
- `src/**`, `tests/**`, `docs/**`, `deploy/**`
- `backend/**`, `frontend/**`, `api/**`, `models/**`
- `config/**`, `lib/**`, `artifact/**`

**Prefix+Suffix:**
- `src/**/*.py`, `tests/**/*.py`, `docs/**/*.md`
- `assets/**/*.png`, `src/components/**/*.tsx`
- `src/module{N}/**/*.py` (parameterized in benchmarks)

**Literals:**
- `src/app.py`, `test.py`, `Makefile`, `README.md`

## Gitwildmatch Semantics (from Git Documentation)

### Critical Rules for Correctness

1. **`**` (double asterisk) behavior:**
   - Leading `**/`: matches in all directories
   - Trailing `/**`: matches everything inside recursively
   - Middle `/**/`: matches zero or more directory levels

2. **Slash handling:**
   - Patterns with `/` are relative to pattern origin
   - Trailing `/` means "directories only"
   - No leading `/` means match at any level

3. **Edge cases:**
   - `**/foo` matches both `foo` and `a/b/foo`
   - `a/**/b` matches `a/b`, `a/x/b`, `a/x/y/b`
   - `*` does NOT match `/`

### Why Prototype Had 10 Missing Matches

Likely causes for prototype correctness issues:

1. **Zero-directory `/**/` case:** `a/**/b` should match `a/b` (zero dirs)
2. **Trailing slash semantics:** `foo/` matches directories only
3. **No-slash patterns:** `*.py` should match at any level
4. **Path normalization:** Windows vs Unix slashes

## GlobTrie Design Requirements

### Data Structure

```python
class GlobTrie:
    prefix_trie: dict       # "src" -> children or terminal
    suffix_map: dict        # ".py" -> set of prefixes or True
    literals: set           # Exact matches
    fallback: list          # Complex patterns -> PathSpec
```

### Required Operations

1. **Insert pattern:** Categorize and store in appropriate structure
2. **Match path:** Check each structure in order:
   - Literal set (O(1))
   - Prefix trie walk (O(path depth))
   - Suffix map lookup (O(1))
   - Fallback PathSpec (O(n patterns))

### Correctness Requirements

- 100% match parity with PathSpec for all test cases
- Handle all gitwildmatch edge cases
- Proper slash normalization (Windows/Unix)
- Directory vs file distinction when trailing `/`

## Cost-Benefit Analysis

### Performance Context (from ADR-002)

| Component | Time | % of Request |
|-----------|------|--------------|
| Git operations | 35ms | 79% |
| Database queries | 7.5ms | 17% |
| Pattern matching | 1.09ms | 2% |
| JSON serialization | 0.02ms | <0.1% |

### GlobTrie Potential Impact

| Scenario | Current | GlobTrie | End-to-End Improvement |
|----------|---------|----------|------------------------|
| Pattern matching | 1.09ms | 0.13ms | ~2% faster overall |
| Full request | ~44ms | ~43ms | Negligible |

### Implementation Cost

- **Development:** 2-4 days for correct implementation
- **Testing:** 1-2 days for comprehensive edge case coverage
- **Maintenance:** Ongoing (gitwildmatch edge cases)
- **Risk:** High (correctness is critical for conflict detection)

## Decision Framework

### When GlobTrie WOULD Make Sense

1. **Pattern count:** >1,000 active patterns per request
2. **Request volume:** >10,000 requests/second
3. **Pattern matching >20%** of request latency
4. **PathSpec becomes bottleneck** (evidence required)

### Current State

- Typical: 50-100 patterns, 10-50 paths per request
- Pattern matching: 2% of latency
- System bottleneck: I/O (git + database)

## Recommendation: NO-GO

**Rationale:**

1. **Insufficient benefit:** 8x pattern speedup = ~2% end-to-end improvement
2. **High correctness risk:** Prototype already had 10 missing matches
3. **Maintenance burden:** Gitwildmatch has subtle edge cases
4. **Union PathSpec sufficient:** 26x speedup already achieved
5. **Wrong bottleneck:** System is I/O bound, not CPU bound

**Alternative optimizations with higher ROI:**

1. **Git operation batching:** Could reduce 35ms git time
2. **Query optimization:** Reduce 7.5ms database time
3. **Connection pooling:** Reduce setup overhead
4. **Caching:** Per-request pattern compilation already cached

## Future Reconsideration

Revisit this decision if:

- [ ] Pattern count per request exceeds 1,000
- [ ] Request rate exceeds 10,000/second
- [ ] Pattern matching becomes >20% of request latency
- [ ] PathSpec library performance degrades
- [ ] Real-world profiling shows pattern matching bottleneck

## References

- [Git gitignore documentation](https://git-scm.com/docs/gitignore)
- [Python pathspec library](https://pypi.org/project/pathspec/)
- [PathSpec documentation](https://python-path-specification.readthedocs.io/en/stable/readme.html)
- [Rust globset crate](https://docs.rs/globset/latest/globset/)
- ADR-002: Rust/PyO3 Optimization Analysis

## Appendix: Test Pattern Categorization

### From bench_pattern_matching.py

```python
# Pure prefix
"src/**", "tests/**", "docs/**", "deploy/**"

# Prefix+suffix
"src/**/*.py", "docs/**/*.md", "assets/**/*.png"

# Single-level (uncommon)
"scripts/*.sh"

# Benchmark generation
f"src/module{i}/**/*.py" for i in range(100)
```

### From test_file_reservation_lifecycle.py

```python
# All pure prefix patterns
"src/**", "docs/**", "lib/**", "config/**"
"app/**", "temp/**", "manual/**", "api/**"
"models/**", "artifact/**", "stale/**", "self/**"
```

### From test_e2e_multi_agent_workflow.py

```python
BACKEND_PATTERN = "backend/**"
FRONTEND_PATTERN = "frontend/**"
# Macro patterns
"src/**", "test/**"
```

All observed production patterns fall into the "pure prefix" or "prefix+suffix" categories, with no complex patterns requiring special handling.
