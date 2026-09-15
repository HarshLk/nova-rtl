# NOVA-RTL Judge Demo Runbook

Use only a verified M9 release directory. Start with `nova m9 verify`, then run
`nova replay <release>/replay --offline --verify-all-artifacts`. The replay must
report zero external calls and the same model hash as `nova demo --headless`.

| Time | View | Evidence shown |
|---:|---|---|
| 0:00–1:00 | Design Health | Five masters, 105 generated clocks, CDC and calibrated cell count |
| 1:00–2:00 | Critical-Path Explorer | Source-mapped failing setup path and editable opportunity |
| 2:00–3:30 | AI Design Review | Blinded proposals, critics, revisions and deterministic disposition |
| 3:30–4:45 | Candidate Tournament | Registered transform, ordered gates and immutable candidate lineage |
| 4:45–6:15 | Recovery Intelligence | Real failed candidate, typed cause and bounded safe recovery |
| 6:15–7:45 | Formal Proof | Strict equivalence plus binding, clock and CDC invariants |
| 7:45–9:15 | Results | Setup/hold, physical PPA, frequency sweep and honest outcome label |
| 9:15–10:00 | Artifact drill-down | Raw evidence hashes, offline replay and engineering utility |

Degraded rehearsal: disable provider credentials and network before replay; the
recorded story must remain identical. For a simulated live EDA timeout, show the
typed infrastructure failure and switch to the verified replay. Never claim a
negative or inconclusive result as an improvement.
