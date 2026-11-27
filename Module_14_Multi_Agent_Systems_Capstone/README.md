# Module 14: Multi-Agent Systems and Applications

## Capstone: Atlas, a Multi-Agent Collaborative System

The capstone was assigned at the start of the course and builds on Modules 10 to 14. I put it here because my project option was the multi-agent system. Modules 10 to 13 each have a short README pointing to the parts of Atlas that use that module's ideas.

Assignment: AI Agent Creation, Option 3 (multi-agent collaborative system). Group "Atlas", sole member Trilok Kalani.

## Files

- `Trilok_Proposal_TrilokKalani_Group_ITAI2376.pdf` - project proposal
- `Trilok_Report_TrilokKalani_Group_ITAI2376.pdf` - final report
- `Trilok_Demo_TrilokKalani_Group_ITAI2376.mp4` - demo video (about 3 minutes)
- `code/atlas/` - the source code. Setup steps are in `code/atlas/README.md`. This is the de-personalized version: names, emails and private config were replaced with placeholders, and no `.env`, data or logs are included.

## What it is

A CEO agent leads eleven specialist agents. They share one long-term memory in Postgres with pgvector, run through one ReAct tool-use loop, and every tool call goes through gates: the tool must exist, a policy table must allow it (anything not listed is refused), outward actions are recorded in a ledger so a retry can never do them twice, and messages or anything that leaves the system wait for my approval.

## Results

- 1,585 tests passed, 7 skipped, 3 failed (all three from the test environment, explained in the report)
- 4 out of 4 live scenarios passed: a multi-part request handed to two specialists, a refused money transfer, a prompt injection inside a pasted email, and feedback stored as a lesson
- Honest weak spot: on a holdout set the job-mail classifier only got role extraction right 40% of the time

## What I took away

Most of the work in a useful agent was not the prompt. It was the fences around it: the policy, the approvals, the ledger and the budget caps. Leader-based coordination was also much easier to debug than agents talking freely to each other.

## Background reading (the ideas Atlas is built on)

- Yao, S., et al. (2023). ReAct: Synergizing reasoning and acting in language models. ICLR. arXiv:2210.03629
- Anthropic Messages API and tool use documentation, https://docs.claude.com
- Cormack, G., Clarke, C., and Buettcher, S. (2009). Reciprocal rank fusion. SIGIR
