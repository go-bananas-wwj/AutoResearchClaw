import json
import pytest
from researchclaw.ideation.transport import parse_json_transport, unchanged_scalars


def test_complete_object_with_bounded_extra_closers_is_recovered():
    assert parse_json_transport('{"a":[1,2]}}')=={'a':[1,2]}
    assert parse_json_transport('```json\n{"a":"unchanged"}\n```')=={'a':'unchanged'}


def test_truncated_or_multiple_responses_are_never_accepted():
    for raw in ('{"a":[1,2','{"a":1}{"b":2}','{"a":1} trailing words','{"a":1}}}}}'):
        with pytest.raises(ValueError):parse_json_transport(raw)


def test_syntax_correction_must_preserve_scientific_values_and_keys():
    raw='{"request_id":"same","metrics":[1,2] "claim":"unchanged"}'
    assert unchanged_scalars(raw,{'request_id':'same','metrics':[1,2],'claim':'unchanged'})
    assert not unchanged_scalars(raw,{'request_id':'same','metrics':[1,3],'claim':'unchanged'})
    assert not unchanged_scalars(raw,{'request_id':'same','metrics':[1,2],'claim':'rewritten'})
    assert not unchanged_scalars(raw,{'metrics':[1,2],'claim':'unchanged'})
