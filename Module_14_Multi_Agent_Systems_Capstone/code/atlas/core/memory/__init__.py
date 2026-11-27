"""Atlas memory API.

Public surface across the five memory systems. Agents should import from
here rather than reaching into submodules so we can refactor internals
without ripping up every callsite.

  from core.memory import add_entity, search_facts, start_episode, ...

Working memory lives in-process per agent run and isn't exposed here.
"""

from __future__ import annotations

from core.memory.embeddings import EmbeddingError, embed
from core.memory.episodic import (
    end_episode,
    get_recent_episodes,
    search_episodes,
    start_episode,
)
from core.memory.procedural import (
    load_procedure,
    search_procedures,
    sync_from_disk,
)
from core.memory.prospective import (
    cancel_trigger,
    create_trigger,
    get_due_triggers,
    mark_fired,
)
from core.memory.semantic import (
    add_attribute,
    add_entity,
    add_fact,
    add_relationship,
    find_entities_by_name,
    get_entity,
    search_facts,
)

__all__ = [
    "embed",
    "EmbeddingError",
    "add_entity",
    "add_attribute",
    "add_relationship",
    "add_fact",
    "search_facts",
    "get_entity",
    "find_entities_by_name",
    "start_episode",
    "end_episode",
    "search_episodes",
    "get_recent_episodes",
    "sync_from_disk",
    "search_procedures",
    "load_procedure",
    "create_trigger",
    "get_due_triggers",
    "mark_fired",
    "cancel_trigger",
]
