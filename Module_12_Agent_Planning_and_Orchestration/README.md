# Module 12: Agent Planning and Orchestration

No separate assignment. In my capstone, planning shows up in two places: the CEO agent splits a request and dispatches the parts to specialists, and the night shift plans first (proposals at night), runs only the approved plans, and reports in the morning. Fixed multi-step jobs run as YAML workflows that pass each step's output to the next.

- Where it is in the code: `core/runtime/dispatcher.py`, `core/night.py`, `core/workflow_engine.py`, `workflows/` in [Module 14, capstone code](../Module_14_Multi_Agent_Systems_Capstone/code/atlas)
