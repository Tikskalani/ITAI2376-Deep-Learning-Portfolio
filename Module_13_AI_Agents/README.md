# Module 13: AI Agents

No separate assignment. Every agent in my capstone runs through the same tool-use loop, and every tool call passes four gates: the tool must exist, the policy must allow it (anything unlisted is refused), outward actions are recorded in a ledger so they never run twice, and anything that leaves the system waits for my approval.

- Where it is in the code: `core/runtime/agent.py`, `core/runtime/policy.py`, `core/runtime/ledger.py`, `core/runtime/approval.py`, and each agent's prompt in `agents/<name>/system.md`, in [Module 14, capstone code](../Module_14_Multi_Agent_Systems_Capstone/code/atlas)
