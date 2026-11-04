"""ExperimentSpec：阶段9静态校验指标方向、真实数据使用、方法清单。"""
import json

from researchclaw.pipeline.experiment_spec import (build_spec, manifest_summary, normalize_direction,
                                                   primary_direction, validate_spec, write_spec)

REQ = [{'id': 'M1', 'description': 'd', 'acceptance_test': 't', 'source_quote': 'q'}]


def _plan(**kw):
    plan = {'execution_target': {'metric_key': 'CRPS', 'direction': 'minimize'},
            'metrics': [{'metric_key': 'CRPS', 'direction': 'minimize'}],
            'method_requirements': REQ,
            'execution': {'validation_level': 'synthetic', 'datasets': [{'id': 's', 'kind': 'synthetic'}]}}
    plan.update(kw)
    return plan


def test_direction_normalization():
    assert normalize_direction('lower_better') == 'minimize' and normalize_direction('maximize') == 'maximize'
    assert normalize_direction('closer_to_nominal') is None


def test_contradictory_primary_direction_rejected():
    # The real stage-09 failure: execution_target maximize vs scoring_direction lower_better.
    plan = _plan(execution_target={'metric_key': 'CRPS', 'direction': 'maximize'},
                 metrics=[{'metric_key': 'CRPS', 'direction': 'maximize', 'scoring_direction': 'lower_better'}])
    spec = build_spec(plan, {}, {}, metric_key='CRPS', metric_direction='minimize')
    errors = validate_spec(spec, plan)
    assert any('方向矛盾' in e for e in errors)


def test_missing_direction_rejected():
    plan = _plan()
    spec = build_spec(plan, {}, {}, metric_key='CRPS', metric_direction='')
    assert any('方向未确定' in e for e in validate_spec(spec, plan))


def test_synthetic_only_plan_rejected_when_real_data_available(tmp_path):
    root = tmp_path / 'real'
    root.mkdir()
    (root / 'data_manifest.json').write_text(json.dumps({'entries': [
        {'id': 'a', 'kind': 'insar_product', 'bytes': 10, 'site': 'xinmo'},
        {'id': 'r', 'kind': 'reference', 'source_url': 'https://doi.org/x', 'failure_date': '2017-06-24', 'site': 'xinmo'}]}))
    summary = manifest_summary(root)
    assert summary['entries'] == 2 and summary['sites'] == ['xinmo'] and summary['references'][0]['failure_date']
    plan = _plan()
    spec = build_spec(plan, {}, {'real_data': [summary]}, metric_key='CRPS', metric_direction='minimize')
    assert any('只使用合成数据' in e for e in validate_spec(spec, plan))
    plan['execution'] = {'validation_level': 'real', 'datasets': [{'id': 'r', 'kind': 'local', 'path': str(root)}]}
    spec = build_spec(plan, {}, {'real_data': [summary]}, metric_key='CRPS', metric_direction='minimize')
    assert validate_spec(spec, plan) == []


def test_spec_roundtrip_and_direction_lookup(tmp_path):
    plan = _plan()
    spec = build_spec(plan, {'topic': 't'}, {}, metric_key='CRPS', metric_direction='minimize')
    assert spec['method_requirements'][0]['passes'] is False
    write_spec(tmp_path, spec)
    assert primary_direction(tmp_path, 'maximize') == 'minimize'
    assert primary_direction(tmp_path / 'missing', 'maximize') == 'maximize'


def test_manifest_summary_tells_code_how_to_find_files(tmp_path):
    import json as _json
    from researchclaw.pipeline.experiment_spec import manifest_summary
    run = tmp_path / "runs" / "rc-x"
    run.mkdir(parents=True)
    entries = [{"id": f"a/{n}", "kind": "insar_product", "path": f"shared/p/S1_1_IW2_ABCD_{n}.tif", "bytes": 1}
               for n in ("wrapped_phase", "corr")] * 2
    (run / "data_manifest.json").write_text(_json.dumps({"files_root": str(tmp_path), "entries": entries}))
    s = manifest_summary(run)
    assert s["files_root"] == str(tmp_path) and "files_root" in s["path_rule"]
    assert s["file_variants"]["insar_product"] == {"wrapped_phase.tif": 2, "corr.tif": 2}
    assert len(s["entry_examples"]["insar_product"]) == 2
