# Where the engine came from

`apex_engine.py` and `apex_domains.py` are copied unmodified from APEX
(`C:\Users\you\Portfolio\Apex 10`, Trilok's own project), tag `apex-v11-final`,
commit `294ec243a90384bc7defcd1374375ccb3896f06f`.

| File | SHA-256 |
|---|---|
| `apex_engine.py` | `633bf99bc98f98cdcde356042937a15a00f4e6d8110f39b3dcbf10f3ccb888c3` |
| `apex_domains.py` | `c934cb444e89871629bcccc42f388795604803e241631565647185830b96aadf` |
| `evals/resume_bench/apex_benchmark.py` (APEX `tools/benchmark.py`) | `fc759340ddb701e047d655dc199dd15a2199e4435e84cbffc4cbb26673c7eb4b` |

`tests/test_resume_engine_provenance.py` fails if either file drifts from these hashes.
To change the engine, change it in APEX, retag, copy again and update this table; the
Atlas side (format conversion, the model bridge, the career tools) lives in other modules
of this package so the copies stay clean.

The engine-only tests came across too. Only their imports and the fixture path changed:

| APEX | Atlas |
|---|---|
| `tests/test_engine.py` | `tests/test_resume_engine.py` |
| `tests/test_engine_flow.py` | `tests/test_resume_engine_flow.py` |
| `tests/test_engine_gate.py` | `tests/test_resume_engine_gate.py` |
| `tests/test_engine_parse.py` | `tests/test_resume_engine_parse.py` |
| `tests/test_quality_fixes.py` (engine tests only) | `tests/test_resume_engine_quality.py` |
| `tests/test_vault.py` (engine and `apex_domains` tests only) | `tests/test_resume_engine_vault.py` |
| `tests/e2e/fixtures/marcus_delgado.txt` (synthetic) | `tests/fixtures/resume_engine/marcus_delgado.txt` |
| `tests/test_engine_benchmark.py` | `tests/test_resume_engine_benchmark.py` |
| `bench/cases/*` (nine synthetic people) | `evals/resume_bench/cases/` (LF line endings) |

`apex_benchmark.py` is kept for its pure helpers (`invented`, `fabrication_audit`, `summarize`); its
`run()` imports APEX's server and is never called in Atlas. The offline bench that uses the helpers is
`evals/resume_bench/__init__.py` (scripted replies, no API).

Not ported: `agent_server.py`, `index.html`, `apex_store.py`, `apex_apply.py`, the live bench runners
(`answer_bench.py`, `feature_bench.py`), and every test that takes the APEX `server` fixture or the store.
