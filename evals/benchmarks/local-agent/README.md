# yucode local-agent benchmark

This benchmark contains 25 coding tasks and 10 local safety/HITL tasks. Every
task is an independent source fixture with a hidden deterministic grader, a
base-fails contract, and a behaviorally valid gold patch. Formal suites run
three repetitions in Provider-only Agent containers; graders always run with
Docker networking disabled.

Validate manifests only:

    python -m evals validate evals/benchmarks/local-agent/full.toml

Validate base/gold contracts without a model call (requires Docker):

    python -m evals validate evals/benchmarks/local-agent/full.toml --baselines

Regenerate checked-in fixtures after editing this script:

    python evals/benchmarks/local-agent/generate_fixtures.py
