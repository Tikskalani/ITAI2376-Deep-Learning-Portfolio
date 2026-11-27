# Module 11: Reasoning Models, RAG and Reasoning Agents

No separate assignment. In my capstone, Atlas uses retrieval before it answers: agents call `recall_facts`, which searches long-term memory in Postgres with pgvector and merges vector, keyword and recency rankings with Reciprocal Rank Fusion. Reasoning runs as a ReAct loop (think, call a tool, read the result, repeat).

- Where it is in the code: `core/memory/retrieval.py`, `core/memory/semantic.py`, `core/runtime/agent.py` in [Module 14, capstone code](../Module_14_Multi_Agent_Systems_Capstone/code/atlas)
