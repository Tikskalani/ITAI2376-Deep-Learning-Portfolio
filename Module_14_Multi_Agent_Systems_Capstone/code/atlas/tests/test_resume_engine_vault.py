"""The evidence vault: facts the candidate confirmed once are kept and reused as evidence
in every later resume (engine) and saved from the answers they give (server)."""
import pytest

from core.resume_engine import apex_engine as E

RESUME = ('JO DOE\n\nEXPERIENCE\nAI Engineer - Acme\n2021 - Present\n- Built a support chatbot on GPT-4\n'
          '- Wrote evals for the chatbot\n\nPROJECTS\nDocChat - RAG over PDFs\n- Answers questions over uploaded PDFs\n')


def test_facts_join_the_evidence_of_their_role_or_project():
    d = E.Doc(RESUME)
    d.attach_facts([{'topic': 'Acme', 'text': 'The chatbot handled 12,000 chats a month.'},
                    {'topic': 'docchat', 'text': 'DocChat has 30 GitHub stars.'},
                    {'topic': '', 'text': 'Speaks Spanish.'}])
    bullet = next(b for b in d.blocks if 'support chatbot' in b['text'])
    project = next(b for b in d.blocks if 'uploaded PDFs' in b['text'])
    assert '12,000 chats' in d.group_text(bullet['id']) and '30 GitHub stars' not in d.group_text(bullet['id'])
    assert '30 GitHub stars' in d.group_text(project['id'])
    assert 'Speaks Spanish' in d.evidence_text() and 'Speaks Spanish' not in d.text
    shown = d.numbered()
    assert 'FACTS THE CANDIDATE CONFIRMED' in shown and '12,000 chats' in shown and 'Speaks Spanish' in shown


def test_no_facts_no_change():
    d = E.Doc(RESUME)
    before = d.numbered()
    d.attach_facts(None)
    assert d.numbered() == before and d.evidence_text() == d.text


@pytest.mark.parametrize('title,ok', [
    ('Senior AI Engineer', True), ('Applied Scientist, LLM', True), ('Member of Technical Staff', True),
    ('Forward Deployed Architect', True), ('Head of AI Native Operations', False),
    ('AI Transformation Owner, Product & Design', False), ('Manager, Applied AI Architects', False),
    ('VP, Applied AI/ML Research', False)])
def test_the_agent_proposes_hands_on_roles(title, ok):
    from core.resume_engine import apex_domains
    assert apex_domains.builder(title) is ok
