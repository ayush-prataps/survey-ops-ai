# Evaluation

## Method
30 to 50 blind fault-injection trials with known ground truth. Faults are injected by `eval/faults/`, labels written to `eval/ground_truth/` (separate store, agents can never read it). `eval/scorer/` compares diagnosis to ground truth after each run.

## Fault set (draft, label each noisy or silent)
| Fault | Type | Alert path |
|---|---|---|
| Connection pool exhaustion | noisy | latency rule |
| Retry storm without backoff | noisy | error/latency rule |
| Missing DB index / N+1 | silent-ish (slow, no errors) | latency rule |
| Race condition in idempotency check | silent | invariant counter |
| Off-by-one pagination | silent | synthetic probe |
Add more to reach 30 to 50 trials (multiple trials per fault type, varied severity and endpoint).
Include plenty of ambiguous and silent faults. If every fault has a self-explanatory error line, the benchmark accidentally proves the Diagnostician is unnecessary.

## Baseline (ablation)
Single LLM call given the same logs and metrics, no loop, no experiments. Expect it to match the loop on easy explicit-error faults. The loop must win on ambiguous and silent ones.

## Metrics
- Accuracy of root cause
- Calibration (does confidence track correctness)
- Abstention correctness (abstains when it should, not when it shouldn't)
- Time-to-detect, time-to-diagnosis, time-to-PR
- Iterations used
- Tokens per run and notional cost at list price (even on free tier)
- Loop vs baseline on all of the above. Expect the loop to cost several times more, so show the accuracy gain on hard faults justifies it.
- Optional: Surface Mapper recall (did a proposition exist for the true fault)

## Notes
- Gemini free tier is rate-limited and Pro-tier has very low request limits. Route high-volume calls to a lighter tier, reserve the stronger tier for Diagnostician reasoning. Check actual limits in AI Studio for your project.
- Example latency numbers elsewhere in docs are illustrative. Report only measured values.