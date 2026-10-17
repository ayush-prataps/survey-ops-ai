# Viva Q&A

Answer honestly. Do not oversell.

**Why not one LLM call over the logs?**
Error lines are symptoms. Many faults produce no error line (race condition, pagination, missing index). The loop runs experiments that discriminate between causes. Our ablation compares against a single-LLM baseline, and we expect the baseline to match on easy faults and lose on ambiguous and silent ones.

**Why separate agents?**
By authority, not topic. Acting and interpreting are split so the system cannot shape experiments to confirm its own belief. Each agent's blast radius is explicit and auditable.

**Could ADK do all four agents?**
Yes. The split reflects task shape (loop and state for ADK, single-pass for CrewAI) and the course requires both frameworks.

**Isn't detection basic? Is it agentic?**
Detection is deliberately deterministic. Our contribution is diagnosis. An LLM polling logs would burn quota and be less reliable than a threshold rule.

**What if there is no error line?**
Silent faults use invariant-check metrics or synthetic probes as alert sources. Be ready to say which faults are in scope.

**How does the Surface Mapper get the dependency graph?**
A plain code tool using Python `ast` plus detection of cross-service HTTP calls. No LLM in graph construction. The LLM reasons over graph plus spec afterward. Limitation: dynamic calls are missed.

**Why is the Surface Mapper isolated from runtime data?**
So it cannot rationalize backward from a known failure. Its propositions are a first-principles map.

**Which failure mode does it think is most likely?**
None. The list is unranked on purpose. Ranking happens in the Diagnostician once evidence exists.

**Who watches the logs?**
Nobody continuously. Prometheus and Alertmanager watch metrics. The Diagnostician is the first agent to act, started by `/investigate`. The Runner reads logs only when asked.

**What is `/investigate`?**
An endpoint on our own backend that Alertmanager calls. Payload is symptom only.

**How is the trial blind?**
Ground-truth label goes to a separate store that agents cannot read. Only the scorer reads it after the run.

**How do you satisfy A2A interoperability?**
Diagnostician (ADK) hands the validated root cause to the Remediator (CrewAI) over A2A.

**How do you handle prompt injection?**
Logs are untrusted text. The Runner sanitizes and delimits them, and the Diagnostician treats delimited content as data only.

**What does it cost and how long does it take?**
Report measured tokens, notional cost and time-to-diagnosis per trial, loop versus baseline. Never quote illustrative numbers as measured.

**Why does the Remediator never merge?**
A wrong auto-merged patch in production is the worst failure. Human review is the safety boundary.

**Why two representations of the repo (graph and semantic index)?**
Graph gives structure for predicting failures. Semantic index finds code by meaning for localizing the patch. The proposition's `graph_edge_ref` gives the Remediator a head start.

**Why is the ground-truth label `connection_pool_exhaustion` when Toxiproxy injected DB latency?**
DB latency is the environmental trigger/stressor; the application's small pool size and missing timeout handling are the architectural weakness. In real systems, external slowdowns trigger connection pool starvation, which engineers remediate in software (tuning pool size, overflow, timeouts). Later benchmark trials will also inject direct connection starvation without latency, and the scorer matches within root-cause failure families.

**Limitations to admit**
Static analysis is incomplete. Detection needs alertable symptoms. Synthetic target app. Free-tier rate limits. Small benchmark.