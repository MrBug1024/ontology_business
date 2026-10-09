"""Worker diagnostics must exclude payloads, source lines and exception text."""
from app.services.compilation_failure_diagnostics import structural_failure


def test_materialization_diagnostic_keeps_type_and_position_without_exception_payload():
    try:
        raise ValueError('synthetic-private-input=never-copy-this')
    except ValueError as error:
        diagnostic = structural_failure(error)
    assert diagnostic['exception_type'] == 'ValueError'
    assert diagnostic['code'] == 'candidate_materialization_failed'
    assert diagnostic['frames'][-1]['file'] == 'test_compilation_failure_diagnostics.py'
    assert diagnostic['frames'][-1]['function'] == 'test_materialization_diagnostic_keeps_type_and_position_without_exception_payload'
    assert diagnostic['frames'][-1]['line'] > 0
    assert 'never-copy-this' not in str(diagnostic)
    assert set(diagnostic['frames'][-1]) == {'file', 'function', 'line'}
