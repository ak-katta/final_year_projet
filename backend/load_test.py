"""
Load / stress testing — fire many concurrent requests at the target and
measure how the server holds up.

This is the one part of the project that deliberately bypasses the
browser. The goal is to load the *server*, and a few hundred Chromium
pages would saturate the test machine long before the server noticed;
plain HTTP from a thread pool puts the pressure where it belongs.

Only point this at a server you own or are authorised to test — a few
hundred concurrent requests is indistinguishable from a small DoS from
the receiving end, which is why nothing here runs unless it is asked for
explicitly and why the defaults are deliberately modest.
"""

import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from state import (
    QAState, LoadTestResult, LoadSample, TestCase, Step, Bug, ActionType,
    TestStatus, TestCategory, TestArea, TestTechnique, Severity,
)


# Conservative enough to be a meaningful test without behaving like an
# attack. Callers can raise them, but they have to mean it.
DEFAULT_CONCURRENCY = 20
DEFAULT_REQUESTS = 200
MAX_CONCURRENCY = 200
MAX_REQUESTS = 5_000
REQUEST_TIMEOUT_S = 15.0

# A run is judged against these unless the caller says otherwise.
DEFAULT_MAX_ERROR_RATE = 0.05     # 5% of requests may fail
DEFAULT_MAX_P95_MS = 3_000.0      # 95th percentile response time


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Plain enough to be obviously correct, and
    the sample sizes here don't justify interpolating."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1))
    return ordered[k]


def _measure_baseline(url: str, headers: dict) -> float:
    """One unloaded request, so the report can quantify the degradation."""
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, follow_redirects=True) as c:
            t0 = time.perf_counter()
            c.get(url, headers=headers)
            return (time.perf_counter() - t0) * 1000
    except Exception:
        return 0.0


def run(
    url: str,
    concurrency: int = DEFAULT_CONCURRENCY,
    total_requests: int = DEFAULT_REQUESTS,
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE,
    max_p95_ms: float = DEFAULT_MAX_P95_MS,
    timeout_s: float = REQUEST_TIMEOUT_S,
) -> LoadTestResult:
    """Fires `total_requests` at `url`, `concurrency` of them in flight at a time."""
    concurrency = max(1, min(int(concurrency), MAX_CONCURRENCY))
    total_requests = max(1, min(int(total_requests), MAX_REQUESTS))

    headers = {"User-Agent": "qa-agent-load-test/1.0"}
    result = LoadTestResult(
        url=url,
        concurrency=concurrency,
        total_requests=total_requests,
        max_error_rate=max_error_rate,
        max_p95_ms=max_p95_ms,
    )
    result.baseline_ms = _measure_baseline(url, headers)

    samples: list[LoadSample] = []

    # One client per worker: httpx.Client is not thread-safe, and sharing
    # a single connection pool across 200 threads would measure the pool's
    # contention rather than the server's.
    def worker(_: int) -> LoadSample:
        t0 = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout_s, follow_redirects=True) as c:
                resp = c.get(url, headers=headers)
            return LoadSample(
                status=resp.status_code,
                ok=resp.status_code < 400,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return LoadSample(
                status=0,
                ok=False,
                latency_ms=(time.perf_counter() - t0) * 1000,
                error=type(e).__name__,
            )

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker, i) for i in range(total_requests)]
        for f in as_completed(futures):
            samples.append(f.result())
    elapsed = time.perf_counter() - started

    return _summarize(result, samples, elapsed)


def _summarize(result: LoadTestResult, samples: list[LoadSample], elapsed: float) -> LoadTestResult:
    latencies = [s.latency_ms for s in samples]
    ok = [s for s in samples if s.ok]

    result.duration_s = round(elapsed, 3)
    result.completed = len(samples)
    result.succeeded = len(ok)
    result.failed = len(samples) - len(ok)
    result.error_rate = (result.failed / len(samples)) if samples else 0.0
    result.requests_per_second = (len(samples) / elapsed) if elapsed > 0 else 0.0

    if latencies:
        result.latency_min_ms = round(min(latencies), 1)
        result.latency_max_ms = round(max(latencies), 1)
        result.latency_mean_ms = round(statistics.fmean(latencies), 1)
        result.latency_p50_ms = round(_percentile(latencies, 50), 1)
        result.latency_p90_ms = round(_percentile(latencies, 90), 1)
        result.latency_p95_ms = round(_percentile(latencies, 95), 1)
        result.latency_p99_ms = round(_percentile(latencies, 99), 1)

    if result.baseline_ms > 0 and result.latency_p50_ms > 0:
        result.degradation_factor = round(result.latency_p50_ms / result.baseline_ms, 2)

    for s in samples:
        key = str(s.status) if s.status else "connection_error"
        result.status_counts[key] = result.status_counts.get(key, 0) + 1
        if s.error:
            result.errors[s.error] = result.errors.get(s.error, 0) + 1

    reasons = []
    if result.error_rate > result.max_error_rate:
        reasons.append(
            f"error rate {result.error_rate:.1%} exceeds the {result.max_error_rate:.0%} threshold"
        )
    if result.latency_p95_ms > result.max_p95_ms:
        reasons.append(
            f"p95 latency {result.latency_p95_ms:.0f}ms exceeds the {result.max_p95_ms:.0f}ms threshold"
        )

    result.passed = not reasons
    if reasons:
        result.verdict = "Server degraded under load: " + "; ".join(reasons)
    else:
        result.verdict = (
            f"Server handled {result.concurrency} concurrent clients "
            f"({result.requests_per_second:.0f} req/s) with "
            f"{result.error_rate:.1%} errors and a p95 of {result.latency_p95_ms:.0f}ms"
        )
    return result


def run_for_state(
    state: QAState,
    concurrency: int = DEFAULT_CONCURRENCY,
    total_requests: int = DEFAULT_REQUESTS,
    **kwargs,
) -> LoadTestResult:
    """Load-tests the run's target URL, records the result and files it as a
    test case so it appears in the test table, coverage matrix and report
    alongside everything else."""
    from datetime import datetime

    test = TestCase(
        name=f"Server holds up under {concurrency} concurrent clients",
        category=TestCategory.PERFORMANCE,
        area=TestArea.LOAD,
        technique=TestTechnique.LOAD,
        description=(
            f"Fires {total_requests} HTTP requests at the target with {concurrency} "
            f"in flight at a time and measures error rate, throughput and latency percentiles"
        ),
        expected=(
            f"Error rate stays under {kwargs.get('max_error_rate', DEFAULT_MAX_ERROR_RATE):.0%} "
            f"and p95 latency under {kwargs.get('max_p95_ms', DEFAULT_MAX_P95_MS):.0f}ms"
        ),
        severity_if_fail=Severity.HIGH,
        source="load",
        steps=[Step(
            index=0, action=ActionType.WAIT,
            description=f"Send {total_requests} concurrent requests to {state.url}",
        )],
    )
    test.started_at = datetime.now()
    state.test_plan.append(test)

    try:
        result = run(state.url, concurrency=concurrency,
                     total_requests=total_requests, **kwargs)
    except Exception as e:
        msg = f"load test could not run: {e}"
        test.steps[0].status = TestStatus.ERROR
        test.steps[0].error = msg
        # same bookkeeping an executed test gets, so the result reaches
        # the pass rate instead of sitting outside the tally
        state.finish_test(test.id, TestStatus.ERROR, error=msg)
        state.add_warning(msg)
        raise

    state.load_test = result

    status = TestStatus.PASSED if result.passed else TestStatus.FAILED
    test.steps[0].status = status
    test.steps[0].duration_ms = result.duration_s * 1000
    if not result.passed:
        test.steps[0].error = result.verdict
    state.finish_test(test.id, status, error=result.verdict if not result.passed else None)
    test.duration_ms = result.duration_s * 1000

    if not result.passed:
        state.add_bug(Bug(
            test_id=test.id,
            title="Server degrades under concurrent load",
            description=(
                f"{result.completed} requests at concurrency {result.concurrency} "
                f"over {result.duration_s:.1f}s."
            ),
            severity=Severity.HIGH,
            category=TestCategory.PERFORMANCE,
            expected=test.expected,
            actual=(
                f"{result.verdict}\n"
                f"throughput: {result.requests_per_second:.1f} req/s\n"
                f"latency p50/p90/p95/p99: {result.latency_p50_ms:.0f}/"
                f"{result.latency_p90_ms:.0f}/{result.latency_p95_ms:.0f}/"
                f"{result.latency_p99_ms:.0f} ms (unloaded baseline "
                f"{result.baseline_ms:.0f} ms)\n"
                f"status codes: {result.status_counts}\n"
                f"errors: {result.errors or '(none)'}"
            ),
            url=state.url,
            steps_to_reproduce=[
                f"Send {result.total_requests} GET requests to {state.url}",
                f"Keep {result.concurrency} requests in flight at a time",
                "Measure error rate and latency percentiles",
            ],
            fingerprint=f"load-degradation|{state.url}",
        ))
    return result
