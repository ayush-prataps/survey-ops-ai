# Overview

## Problem
When an API fails in production, an engineer manually does four slow tasks: correlate logs, metrics and error rates to guess a root cause; find the responsible file in the codebase; hand-write a regression test; write the postmortem. SurveyOps automates this loop.

## Users
Backend and DevOps teams running multi-service API systems who need faster, evidence-backed root-cause diagnosis during incidents.

## Core idea
Agents are separated by authority and access, not by topic. The diagnostic loop (hypothesize, challenge, re-evaluate) actively tries to falsify its own theories instead of trusting the first plausible one. The system may legitimately abstain.

## Objectives
1. Four-agent architecture where separation is justified by authority/access boundaries, so each agent's blast radius is explicit and auditable.
2. Evidence-driven diagnostic loop that seeks to falsify its own hypotheses.
3. Rigorous evaluation via blind fault injection against known ground truth, with a single-LLM baseline for ablation.

## Expected outcome
Given a live but unlabeled fault, the system converges on the correct root cause or correctly abstains, and proposes a human-reviewed fix as a real PR. Report accuracy, calibration, abstention correctness, latency and cost over 30 to 50 blind trials.

## Non-goals
- No continuous LLM monitoring of logs (detection is deterministic).
- No auto-merge, ever.
- No local/open-weight LLM in the core build (possible stretch for the Runner).
- No real user data or PII. Target app is self-built and synthetic.
- Not a production incident tool. It is a simulated production.

## Course context
CSE3101 Agentic AI, BML Munjal University, 7th semester 2026-27. Project-based, evaluated in three phases. See `03-REQUIREMENTS.md`.