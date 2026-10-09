"""A deliberate discussion answers questions without promoting or rewriting source."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.plugin_coding_repair import code_and_repair


def discussion_runner(steps):
    source = {'round_mode': 'discuss', 'files': {'README.md': 'Unreviewed source'},
              'phase': 'generating', 'round_base_phase': 'validation_failed',
              'validation': ['An existing issue']}
    original = deepcopy(source)
    checkpoints = []

    def checkpoint(step=None, *, finish=False, **kwargs):
        checkpoints.append({'step': step, 'finish': finish})
        return deepcopy(source)

    def generate(document, instruction):
        assert instruction == 'Explain the saved project'
        yield from [SimpleNamespace(**item) for item in steps]

    code_and_repair(source, 'Explain the saved project', generate=generate,
                    checkpoint=checkpoint, step_factory=SimpleNamespace)
    assert source == original
    return checkpoints


def test_explicit_discussion_can_answer_without_generating_files_or_marking_source_valid():
    calls = discussion_runner([{'kind': 'plan', 'message': 'Read the saved files'},
                               {'kind': 'summary', 'message': 'The plugin collects inputs and uses the trusted adapter.'}])
    assert [item['step'].kind for item in calls if item['step']] == ['plan', 'summary']
    assert calls[-1]['finish'] is True


@pytest.mark.parametrize('steps', [[], [{'kind': 'plan', 'message': 'Only a plan'}],
                                  [{'kind': 'summary', 'message': '  '}],
                                  [{'kind': 'file', 'message': 'Unexpected edit'}]])
def test_discussion_requires_an_answer_and_refuses_candidate_file_edits(steps):
    with pytest.raises(ValueError):
        discussion_runner(steps)
