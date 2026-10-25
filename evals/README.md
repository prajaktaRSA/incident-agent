# Evals

Evals check that the **agent reaches the right conclusion**, not that the code works (that's
`tests/`). They run real investigations, call the LLM, cost money and produce scores.

| | `tests/` | `evals/` |
|---|---|---|
| Checks | Code correctness | Quality of the RCA |
| LLM | Scripted fake | Real model |
| Result | Pass / fail, deterministic | Scores per scenario, can vary run to run |
| Runs | Every change (`uv run pytest`) | Before merging prompt, rule or model changes |

## Layout
- `expectations.yaml`: the expected outcome for each scenario (category, confidence, required
  citations and mentions).
- `reports/` *(git-ignored, M3)*: one report per run with per-scenario results and cost.

The runner (`incident-agent eval`) arrives in M3. The triggers and mock data it uses live in
`fixtures/`.
