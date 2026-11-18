"""Independent local expectations for declarative workbench compilation."""
import importlib
import json
import uuid

import pandas as pd
import pytest


@pytest.fixture
def api():
    return importlib.import_module('tools.workbench_analysis')


def frame():
    return pd.DataFrame({'地区': ['乙', '甲', '乙', '甲'], '收入': [10, 20, 7, -2],
                         '支出': [3, 4, 2, 1], '比率': [0.5, 1.25, -0.25, 2.0]})


def spec(**changes):
    value = {'version': 1, 'group_by': ['地区'], 'filters': [],
             'metrics': [{'id': 'm1', 'op': 'sum', 'column': '收入', 'label': '收入合计'},
                         {'id': 'm2', 'op': 'sum', 'column': '支出', 'label': '支出合计'}],
             'derived': [{'id': 'd1', 'op': 'difference', 'left': 'm1', 'right': 'm2', 'label': '净额'}],
             'sort': [], 'limit': None}
    value.update(changes)
    return value


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_combined_operations_restore_labels_and_pin_source(api, tmp_path, monkeypatch):
    plan = spec(filters=[{'column': '收入', 'op': 'gt', 'value': 0}],
                sort=[{'key': 'd1', 'direction': 'desc'}], limit=1)
    root = tmp_path / str(uuid.uuid4())
    result = api.run_analysis(frame(), plan, root)
    assert result['run_id'] == root.name
    assert result['rows'] == [{'g0': '甲', 'm1': 20, 'm2': 4, 'd1': 16}]
    assert result['status'] == 'EXECUTED_UNVERIFIED' and result['correctness'] == 'NOT_EVALUATED'
    assert result['source'] == 'local_deterministic'
    assert result['columns'] == [{'key': 'g0', 'label': '地区', 'type': 'string'},
                                 {'key': 'm1', 'label': '收入合计', 'type': 'integer'},
                                 {'key': 'm2', 'label': '支出合计', 'type': 'integer'},
                                 {'key': 'd1', 'label': '净额', 'type': 'integer'}]
    source = (root / 'candidate.py').read_bytes()
    assert source == (root / 'ledger/candidate.bin').read_bytes()
    assert '地区'.encode() not in source and '收入'.encode() not in source
    before = snapshot(root)
    monkeypatch.setattr(api, 'execute', lambda *a, **k: pytest.fail('recovery executed'))
    assert api.recover_analysis(root) == result
    assert snapshot(root) == before


def test_all_aggregates_and_ratio(api, tmp_path):
    metrics = [{'id': 'm1', 'op': 'count', 'column': None, 'label': '行数'}]
    metrics += [{'id': 'm' + str(i + 2), 'op': op, 'column': '收入', 'label': op}
                for i, op in enumerate(['sum', 'mean', 'min', 'max'])]
    result = api.run_analysis(frame(), spec(group_by=[], metrics=metrics,
        derived=[{'id': 'd1', 'op': 'ratio', 'left': 'm2', 'right': 'm1', 'label': '每行'}]), tmp_path / 'run')
    assert result['rows'] == [{'m1': 4, 'm2': 35, 'm3': 8.75, 'm4': -2, 'm5': 20, 'd1': 8.75}]


def test_exact_large_integer_and_separate_float_metrics(api, tmp_path):
    big = pd.DataFrame({'地区': ['甲', '甲'], '收入': [2**63 - 1, 1], '支出': [-1, -2]})
    assert api.run_analysis(big, spec(), tmp_path / 'big')['rows'] == [
        {'g0': '甲', 'm1': 2**63, 'm2': -3, 'd1': 2**63 + 3}]
    floating = pd.DataFrame({'地区': ['甲', '甲'], '收入': [1e16, 1.0], '支出': [1e16, 0.0]})
    result = api.run_analysis(floating, spec(), tmp_path / 'float')
    assert result['rows'] == [{'g0': '甲', 'm1': 1e16, 'm2': 1e16, 'd1': 0.0}]


@pytest.mark.parametrize('groups', [[], ['地区']])
def test_filtered_empty_and_nulls(api, tmp_path, groups):
    plan = spec(group_by=groups, filters=[{'column': '地区', 'op': 'eq', 'value': '不存在'}],
        metrics=[{'id': 'm1', 'op': 'count', 'column': None, 'label': '数量'},
                 {'id': 'm2', 'op': 'mean', 'column': '收入', 'label': '均值'},
                 {'id': 'm3', 'op': 'min', 'column': '收入', 'label': '最小'},
                 {'id': 'm4', 'op': 'max', 'column': '收入', 'label': '最大'}],
        derived=[{'id': 'd1', 'op': 'ratio', 'left': 'm1', 'right': 'm1', 'label': '占比'}])
    result = api.run_analysis(frame(), plan, tmp_path / 'run')
    assert result['status'] == 'EXECUTED_UNVERIFIED'
    assert result['rows'] == ([] if groups else [{'m1': 0, 'm2': None, 'm3': None, 'm4': None, 'd1': None}])
    assert result['warnings']


def test_string_order_filter_numeric_group_and_stable_sort(api, tmp_path):
    data = pd.DataFrame({'标签': ['c', 'a', 'b', 'b'], '组': [2, 1, 3, 1], '值': [4, 4, 4, 4]})
    plan = spec(group_by=['组'], filters=[{'column': '标签', 'op': 'gt', 'value': 'aa'}],
                metrics=[{'id': 'm1', 'op': 'sum', 'column': '值', 'label': '合计'}],
                derived=[], sort=[{'key': 'm1', 'direction': 'desc'}])
    assert api.run_analysis(data, plan, tmp_path / 'run')['rows'] == [
        {'g0': 1, 'm1': 4}, {'g0': 2, 'm1': 4}, {'g0': 3, 'm1': 4}]


def test_zero_ratio_warns_and_null_sorts_last(api, tmp_path):
    data = pd.DataFrame({'地区': ['甲', '乙'], '收入': [5, 7], '支出': [0, 2]})
    plan = spec(derived=[{'id': 'd1', 'op': 'ratio', 'left': 'm1', 'right': 'm2', 'label': '比值'}],
                sort=[{'key': 'd1', 'direction': 'desc'}])
    result = api.run_analysis(data, plan, tmp_path / 'run')
    assert result['rows'][0]['d1'] == 3.5 and result['rows'][1]['d1'] is None
    assert any('0' in warning for warning in result['warnings'])


def test_nonfinite_execution_is_retained_failure(api, tmp_path):
    data = pd.DataFrame({'地区': ['甲', '甲'], '收入': [1e308, 1e308], '支出': [1e308, 1e308]})
    result = api.run_analysis(data, spec(), tmp_path / 'run')
    assert result['status'] == 'EXECUTION_FAILED' and result['rows'] == []
    assert result['reason'] and result['correctness'] == 'NOT_EVALUATED'


@pytest.mark.parametrize('change', ['unknown-column', 'unknown-op', 'bad-ref', 'duplicate-id',
                                   'bool-filter', 'bad-limit', 'too-many-groups', 'unknown-key'])
def test_bad_spec_rejects_before_output(api, tmp_path, monkeypatch, change):
    plan = spec()
    if change == 'unknown-column': plan['group_by'] = ['不在输入']
    if change == 'unknown-op': plan['metrics'][0]['op'] = 'eval'
    if change == 'bad-ref': plan['derived'][0]['left'] = 'd100'
    if change == 'duplicate-id': plan['metrics'][1]['id'] = 'm1'
    if change == 'bool-filter': plan['filters'] = [{'column': '收入', 'op': 'eq', 'value': True}]
    if change == 'bad-limit': plan['limit'] = 0
    if change == 'too-many-groups': plan['group_by'] = list(frame().columns)
    if change == 'unknown-key': plan['python'] = 'import os'
    monkeypatch.setattr(api, 'execute', lambda *a, **k: pytest.fail('invalid plan executed'))
    with pytest.raises(api.WorkbenchError) as error:
        api.run_analysis(frame(), plan, tmp_path / 'run')
    assert error.value.code and error.value.message
    assert not (tmp_path / 'run').exists()


def test_plan_reuse_and_descriptions(api, tmp_path):
    plan = api.validate_spec(frame(), spec())
    text = '\n'.join(api.describe_spec(frame(), plan))
    assert '地区' in text and '收入' in text and '支出' in text
    changed = frame().assign(收入=[1, 2, 3, 4])
    result = api.run_analysis(changed, json.loads(json.dumps(plan)), tmp_path / 'run')
    assert sum(row['m1'] for row in result['rows']) == 10
    with pytest.raises(api.WorkbenchError): api.validate_spec(changed.drop(columns='收入'), plan)


@pytest.mark.parametrize('target', ['candidate.py', 'spec.json', 'binding.json', 'request.json', 'execution.json'])
def test_recovery_tampering_rejected_without_writes(api, tmp_path, monkeypatch, target):
    root = tmp_path / 'run'
    api.run_analysis(frame(), spec(), root)
    path = root / target
    path.write_bytes(path.read_bytes() + b' ')
    before = snapshot(root)
    monkeypatch.setattr(api, 'execute', lambda *a, **k: pytest.fail('recovery executed'))
    with pytest.raises(api.WorkbenchError): api.recover_analysis(root)
    assert snapshot(root) == before


def test_unknown_start_read_only_recovery_no_retry(api, tmp_path, monkeypatch):
    class Crash(BaseException): pass
    def crash(label):
        if label == 'start:published': raise Crash()
    root = tmp_path / 'run'
    monkeypatch.setattr(api, '_boundary', crash)
    with pytest.raises(Crash): api.run_analysis(frame(), spec(), root)
    before = snapshot(root)
    monkeypatch.setattr(api, 'execute', lambda *a, **k: pytest.fail('recovery executed'))
    result = api.recover_analysis(root)
    assert result['status'] == 'UNKNOWN_BLOCKED' and result['rows'] == []
    assert api.recover_analysis(root) == result and snapshot(root) == before


def test_public_candidate_receives_exact_published_text_tokens(tmp_path):
    from tools import workbench_analysis as analysis
    from tools import local_synthetic_workflow as storage
    bundle={'schema':{'schema_version':1,'fields':[{'name':'COL_0001','dtype':'object'}]},
        'examples':[{'COL_0001':'SYNTHETIC_1'},{'COL_0001':'SYNTHETIC_2'}],
        'output':{'columns':[{'key':'group','label':'Group','type':'string','nullable':False,
            'restore_from':'COL_0001'}],'max_rows':10}}
    raw=b'def analyze(df):\n    assert list(df["COL_0001"]) == ["SYNTHETIC_1", "SYNTHETIC_2"]\n    return {"rows":[{"group":str(v)} for v in df["COL_0001"]]}\n'
    result=analysis.run_public_candidate(raw,bundle,tmp_path/'public-run',source='local_test_double')
    assert result['rows']==[{'group':'SYNTHETIC_1'},{'group':'SYNTHETIC_2'}]
    assert storage._json(tmp_path/'public-run'/'request.json')['records']==bundle['examples']
    assert analysis.recover_candidate(tmp_path/'public-run')==result
