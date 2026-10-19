# Task: Reduce Diagnostician token usage (no logic changes)

## Context

The Diagnostician (`surveyops/agents/diagnostician/agent.py`) is a Google ADK
`LoopAgent` with two `LlmAgent` steps (`hypothesis_and_experiment_selector`,
`ledger_evaluator`). It works correctly (5/5 live runs found the right root
cause), but token usage is too high and grows with each loop iteration:

| Run | Iterations | Total tokens |
|-----|-----------|--------------|
| 3   | 1         | ~28.8K       |
| 5   | 2         | ~48.0K       |
| 1   | 2         | ~100.7K      |
| 2   | 3         | ~106.1K      |

(Source: `eval/diagnostician_live_run_summary.json`.)

## Root causes

1. **Conversation history is sent twice.** The instruction builders in
   `surveyops/agents/diagnostician/prompts.py` already embed everything the model
   needs (incident, propositions, hypotheses, ledger, latest observation). But
   neither `LlmAgent` sets `include_contents`, so ADK also sends the full session
   history on every call, including earlier tool calls whose arguments contain the
   full hypotheses and ledger JSON. As a result, the prompt grows with every iteration.
2. **Wasted follow-up LLM call after each tool submission.** After a submission
   tool returns, ADK makes another model call so the LLM can "respond" to the tool
   result. That text is never used (the result is already written to session state).

## Required changes

### Fix 1: `surveyops/agents/diagnostician/agent.py`

In `build_diagnostician_loop_agent`, add `include_contents="none"` to **both**
`LlmAgent(...)` constructors (`hypothesis_agent` and `ledger_evaluator`).

### Fix 2: `surveyops/llm_utils.py`

In `build_adk_submission_tool` → `tool_func`, on the **success path only**
(after state is written, before the success `return`), add:

```python
if tool_context is not None and getattr(tool_context, "actions", None) is not None:
    tool_context.actions.skip_summarization = True
```

Do **not** set it on the validation-error / validation-rejected return paths.
Those must still go back to the LLM so it can correct itself and call the tool again.

## Constraints (do NOT change)

- Do not change prompts, schemas, hypothesis/ledger logic, falsification rules,
  confidence rules, exit logic (`exit_logic.py`), A2A dispatch, or the remediator handoff.
- Do not change the model, temperature, or `max_output_tokens`.
- Keep token tracking (`callbacks.py`) intact so before/after can be compared.
- Google ADK only (no LangChain/LangGraph/LiteLLM), per `AGENTS.md`.

## Things to verify

1. Confirm that the installed `google-adk` version supports `include_contents="none"` on
   `LlmAgent` and `tool_context.actions.skip_summarization`. If either is named
   differently in this version, use the equivalent and note it.
2. **Validation retry still works with Fix 1.** When `submit_ledger_update` returns
   "Validation rejected: ...", the model must still see its rejected call and the
   error within the same step, so it can retry. Confirm that ADK's `"none"` mode keeps
   current-turn events. If it doesn't, revert Fix 1 for `ledger_evaluator` only
   and explain why.
3. The LLM record/replay cache (`diagnostician/cache.py`) is keyed on the request,
   so old cached entries will miss after this change. That's expected; don't
   "fix" it by changing the cache key logic.

## Done when

- `pytest` passes (update tests only if they assert on the exact request contents
  that changed, and explain each test change).
- A live run (`scripts/live_diagnostician_test.py`) still reaches the correct
  root cause, and `total_tokens` per run is clearly lower than the table above,
  especially for 2–3 iteration runs.
- Report: files changed, test results, and before/after token numbers.
