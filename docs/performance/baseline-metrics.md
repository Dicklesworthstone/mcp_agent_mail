# Baseline Performance Metrics

**Git SHA**: da08ae4
**Date**: 2026-01-12
**Phase**: Post Phase 1 (N+1 Query Elimination)

## Summary

These metrics establish the baseline performance after completing Phase 1 optimizations (N+1 query elimination with composite indexes).

## Benchmark Results

### send_message (50 iterations)
| Metric | Value |
|--------|-------|
| Mean Latency | 118.6 ms |
| P50 Latency | 115.4 ms |
| P95 Latency | 176.1 ms |
| P99 Latency | 190.6 ms |
| Throughput | 8.4 ops/sec |
| Peak Memory | 4.1 MB |

**Dataset**: 512 byte messages, 1 recipient

### fetch_inbox (25 iterations)
| Metric | Value |
|--------|-------|
| Mean Latency | 39.3 ms |
| P50 Latency | 38.6 ms |
| P95 Latency | 43.5 ms |
| P99 Latency | 46.7 ms |
| Throughput | 25.4 ops/sec |
| Peak Memory | 2.1 MB |

**Dataset**: 200 messages, 256 byte size, limit 100

### search_messages (15 iterations)
| Metric | Value |
|--------|-------|
| Mean Latency | 12.3 ms |
| P50 Latency | 11.7 ms |
| P95 Latency | 14.3 ms |
| P99 Latency | 15.0 ms |
| Throughput | 81.0 ops/sec |
| Peak Memory | ~2 MB |

**Dataset**: 300 messages, FTS5 query "alpha OR beta"

### summarize_thread (10 iterations)
| Metric | Value |
|--------|-------|
| Mean Latency | 22.8 ms |
| P50 Latency | 23.0 ms |
| P95 Latency | 24.2 ms |
| P99 Latency | 24.5 ms |
| Throughput | 43.8 ops/sec |
| Peak Memory | ~2 MB |

**Dataset**: 40 messages in thread, LLM mode disabled

### list_outbox (20 iterations)
| Metric | Value |
|--------|-------|
| Mean Latency | 3,831.8 ms |
| P50 Latency | 3,726.1 ms |
| P95 Latency | 4,191.7 ms |
| P99 Latency | 4,301.3 ms |
| Throughput | 0.26 ops/sec |
| Peak Memory | ~4 MB |

**Dataset**: 150 messages, 3 recipients, limit 100

## Observations

1. **list_outbox is significantly slower** than other operations (~3.8s vs <120ms). This is a candidate for optimization in Phase 2.

2. **search_messages is fast** (~12ms) thanks to SQLite FTS5 indexing.

3. **fetch_inbox performs well** (~39ms) after Phase 1 N+1 query optimizations.

4. **Memory usage is reasonable** across all operations (2-4 MB peak).

## Test Environment

- Python 3.14.2
- SQLite with FTS5
- aiosqlite async driver
- FastMCP framework

## Verification Tests Status

All Phase 1 verification tests pass:
- E2E isomorphism tests: 3/3 passed
- Query count regression tests: 2/2 passed
