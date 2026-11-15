"""Bounded declarative analyses compiled locally for the existing supervised worker."""
from functools import wraps
import json
import math
import operator
import os
from pathlib import Path
import re
import uuid

import pandas as pd

from runtime.mvp_binding import issue_binding, bind_records, public_schema
from runtime.scoped_enum import restore_value
from runtime.supervised_execution import Limits, execute
from tools import local_synthetic_workflow as storage
from tools.local_analysis_execution import execute_once, recover_ledger
from tools.local_attempt_ledger import LedgerError

TASK = 'local-composable-analysis-v1'
SCOPE = 'local-workbench/composable-analysis'
NAMESPACE = 'local-workbench'
OPS = {'eq': operator.eq, 'ne': operator.ne, 'gt': operator.gt,
       'ge': operator.ge, 'lt': operator.lt, 'le': operator.le}
AGGREGATES = {'count': '计数', 'sum': '求和', 'mean': '平均值', 'min': '最小值', 'max': '最大值'}
WARNINGS = {'empty': '筛选后没有数据；整体计数和求和为 0，平均值、最小值和最大值为空。',
            'zero_ratio': '部分比值的分母为 0，结果显示为空。'}


class WorkbenchError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        self.message = message or '本地分析记录无效或已改变；请保留首次记录，不要覆盖重试。'
        super().__init__(self.message)


def _guarded(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except WorkbenchError:
            raise
        except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError,
                LedgerError, storage.WorkflowError):
            raise WorkbenchError('invalid_local_analysis') from None
    return call


def _require(value, code, message):
    if not value:
        raise WorkbenchError(code, message)


def _fields(value, names, context):
    _require(type(value) is dict and set(value) == set(names.split()),
             'invalid_spec', context + '的字段不完整或包含不支持的字段。')


def _frame(frame):
    _require(isinstance(frame, pd.DataFrame) and 1 <= len(frame) <= 4096 and
             1 <= len(frame.columns) <= 32 and frame.columns.is_unique and
             all(type(c) is str and c.strip() and len(c) <= 128 for c in frame.columns),
             'invalid_input', '输入需要 1～4096 行、1～32 个不重复的有效列名。')
    for column in frame:
        values = frame[column]
        dtype = str(values.dtype)
        _require(dtype in {'object', 'int64', 'float64'} and not values.isna().any() and
                 (dtype != 'object' or all(type(v) is str and bool(v) for v in values)) and
                 (dtype != 'float64' or all(math.isfinite(v) for v in values)),
                 'unsupported_column', '列「' + column + '」需要非空文本、int64 或有限 float64；暂不支持缺失值。')


@_guarded
def validate_spec(frame, spec):
    """Return a canonical allowlisted plan, never code or an executable expression."""
    _frame(frame)
    allowed = {'version', 'group_by', 'filters', 'metrics', 'derived', 'sort', 'limit'}
    _require(type(spec) is dict and set(spec) <= allowed and type(spec.get('version')) is int
             and spec['version'] == 1, 'invalid_spec', '分析方案必须使用版本 1 和支持的配置字段。')
    plan = {'version': 1, 'group_by': spec.get('group_by', []), 'filters': spec.get('filters', []),
            'metrics': spec.get('metrics', []), 'derived': spec.get('derived', []),
            'sort': spec.get('sort', []), 'limit': spec.get('limit')}
    for name in ['group_by', 'filters', 'metrics', 'derived', 'sort']:
        _require(type(plan[name]) is list and len(plan[name]) <= (3 if name == 'group_by' else 16),
                 'spec_size', '分组最多 3 列；筛选、指标、计算列和排序分别最多 16 项。')
    _require(plan['metrics'], 'missing_metrics', '请至少配置一个统计指标。')
    groups = plan['group_by']
    _require(all(type(c) is str and c in frame for c in groups) and len(set(groups)) == len(groups),
             'unknown_column', '分组列不存在或被重复选择。')
    for filt in plan['filters']:
        _fields(filt, 'column op value', '筛选')
        _require(type(filt['column']) is str and filt['column'] in frame and filt['op'] in OPS,
                 'invalid_filter', '筛选列或比较方式不受支持。')
        value = filt['value']
        valid = (type(value) is str if str(frame[filt['column']].dtype) == 'object' else
                 type(value) in (int, float) and math.isfinite(value))
        _require(valid, 'invalid_filter_value', '筛选值的类型必须与列匹配，数值必须有限。')
    ids = set()
    labels = set(groups)
    for metric in plan['metrics']:
        _fields(metric, 'id op column label', '统计指标')
        _require(type(metric['id']) is str and re.fullmatch(r'm[1-9][0-9]{0,2}', metric['id'])
                 and metric['id'] not in ids and metric['op'] in AGGREGATES,
                 'invalid_metric', '统计指标需要唯一编号和支持的计算方式。')
        ids.add(metric['id'])
        _require((metric['op'] == 'count' and metric['column'] is None) or
                 (metric['op'] != 'count' and type(metric['column']) is str and
                  metric['column'] in frame and str(frame[metric['column']].dtype) in {'int64', 'float64'}),
                 'metric_column', '计数不选择数值列；其余统计需要有效数值列。')
    base_ids = set(ids)
    for derived in plan['derived']:
        _fields(derived, 'id op left right label', '计算列')
        _require(type(derived['id']) is str and re.fullmatch(r'd[1-9][0-9]{0,2}', derived['id']) and
                 derived['id'] not in ids and derived['op'] in {'difference', 'ratio'} and
                 derived['left'] in base_ids and derived['right'] in base_ids,
                 'invalid_derived', '计算列需要唯一编号，并引用两个已配置的统计指标。')
        ids.add(derived['id'])
    for item in plan['metrics'] + plan['derived']:
        label = item['label']
        _require(type(label) is str and label.strip() and len(label) <= 80 and label not in labels,
                 'invalid_label', '结果列名称需要非空、互不重复且不超过 80 字。')
        labels.add(label)
    keys = ids | {'g' + str(i) for i in range(len(groups))}
    seen_sort = set()
    for sort in plan['sort']:
        _fields(sort, 'key direction', '排序')
        _require(sort['key'] in keys and sort['key'] not in seen_sort and sort['direction'] in {'asc', 'desc'},
                 'invalid_sort', '排序必须选择不重复的结果列和升序或降序。')
        seen_sort.add(sort['key'])
    _require(plan['limit'] is None or type(plan['limit']) is int and 1 <= plan['limit'] <= 1000,
             'invalid_limit', 'Top N 需要 1～1000 的整数，或不设上限。')
    _require(len(storage._bytes(plan)) <= 65536, 'spec_size', '分析方案超过大小上限。')
    return json.loads(storage._bytes(plan))


def describe_spec(frame, spec):
    plan = validate_spec(frame, spec)
    lines = ['按「' + '、'.join(plan['group_by']) + '」分组。' if plan['group_by'] else '对筛选后的全部行汇总。']
    symbols = {'eq': '等于', 'ne': '不等于', 'gt': '大于', 'ge': '大于等于', 'lt': '小于', 'le': '小于等于'}
    lines += ['筛选：' + f['column'] + ' ' + symbols[f['op']] + ' ' + str(f['value']) for f in plan['filters']]
    labels = {'g' + str(i): column for i, column in enumerate(plan['group_by'])}
    for metric in plan['metrics']:
        labels[metric['id']] = metric['label']
        lines.append(metric['label'] + '：' + AGGREGATES[metric['op']] +
                     ('全部保留行' if metric['column'] is None else '「' + metric['column'] + '」'))
    for item in plan['derived']:
        labels[item['id']] = item['label']
        lines.append(item['label'] + '：' + labels[item['left']] +
                     (' − ' if item['op'] == 'difference' else ' ÷ ') + labels[item['right']] + '（先完成各项汇总）')
    lines += ['按「' + labels[s['key']] + '」' + ('升序' if s['direction'] == 'asc' else '降序') for s in plan['sort']]
    if plan['limit'] is not None:
        lines.append('显示前 ' + str(plan['limit']) + ' 行。')
    lines.append('本地确定性编译；执行不等于数值正确性认证。')
    return lines


_PROGRAM = '''
def analyze(df):
    records = df.to_dict("records")
    selected = []
    for record in records:
        keep = True
        for filt in PLAN["filters"]:
            a, b, op = record[filt["column"]], filt["value"], filt["op"]
            if op == "in": ok = a in b
            elif op == "eq": ok = a == b
            elif op == "ne": ok = a != b
            elif op == "gt": ok = a > b
            elif op == "ge": ok = a >= b
            elif op == "lt": ok = a < b
            else: ok = a <= b
            if not ok: keep = False
        if keep: selected.append(record)
    groups = {} if PLAN["groups"] else {(): []}
    for record in selected:
        key = tuple(record[column] for column in PLAN["groups"])
        groups.setdefault(key, []).append(record)
    rows = []
    warnings = set()
    if not selected: warnings.add("empty")
    for group, records in sorted(groups.items()):
        row = {"g" + str(i): value for i, value in enumerate(group)}
        for metric in PLAN["metrics"]:
            op = metric["op"]
            values = [] if metric["column"] is None else [record[metric["column"]] for record in records]
            if op == "count": value = len(records)
            elif op in ("sum", "mean"):
                value = sum(int(v) for v in values) if metric["integer"] else math.fsum(float(v) for v in values)
                if op == "mean": value = value / len(values) if values else None
            elif op == "min": value = min(values) if values else None
            else: value = max(values) if values else None
            if value is not None and type(value) is float and not math.isfinite(value):
                raise OverflowError("nonfinite aggregate")
            row[metric["id"]] = value
        for item in PLAN["derived"]:
            left, right = row[item["left"]], row[item["right"]]
            if left is None or right is None: value = None
            elif item["op"] == "ratio" and right == 0:
                value = None
                warnings.add("zero_ratio")
            elif item["op"] == "ratio": value = left / right
            else: value = left - right
            if value is not None and type(value) is float and not math.isfinite(value):
                raise OverflowError("nonfinite derived result")
            row[item["id"]] = value
        rows.append(row)
    for order in reversed(PLAN["sort"]):
        key = order["key"]
        populated = [row for row in rows if row[key] is not None]
        missing = [row for row in rows if row[key] is None]
        rows = sorted(populated, key=lambda row: row[key], reverse=order["direction"] == "desc") + missing
    if PLAN["limit"] is not None: rows = rows[:PLAN["limit"]]
    return {"rows": rows, "warnings": sorted(warnings)}
'''


def _compile(frame, plan, binding):
    fields = binding['mapping']['fields']
    config = {'groups': [fields[c] for c in plan['group_by']], 'filters': [], 'metrics': [],
              'derived': [{k: d[k] for k in ['id', 'op', 'left', 'right']} for d in plan['derived']],
              'sort': plan['sort'], 'limit': plan['limit']}
    for filt in plan['filters']:
        item = dict(filt, column=fields[filt['column']])
        if str(frame[filt['column']].dtype) == 'object':
            item['value'] = [token for value, token in sorted(binding['enums'][item['column']]['forward'].items())
                             if OPS[filt['op']](value, filt['value'])]
            item['op'] = 'in'
        config['filters'].append(item)
    columns = [{'key': 'g' + str(i), 'label': c, 'type':
                {'object': 'string', 'int64': 'integer', 'float64': 'number'}[str(frame[c].dtype)]}
               for i, c in enumerate(plan['group_by'])]
    types = {}
    nullable = set()
    for metric in plan['metrics']:
        integer = metric['column'] is None or str(frame[metric['column']].dtype) == 'int64'
        kind = 'number' if metric['op'] == 'mean' or not integer else 'integer'
        types[metric['id']] = kind
        if metric['op'] in {'mean', 'min', 'max'}: nullable.add(metric['id'])
        config['metrics'].append({'id': metric['id'], 'op': metric['op'], 'integer': integer,
                                  'column': fields.get(metric['column'])})
        columns.append({'key': metric['id'], 'label': metric['label'], 'type': kind})
    for item in plan['derived']:
        kind = 'integer' if item['op'] == 'difference' and all(types[k] == 'integer' for k in [item['left'], item['right']]) else 'number'
        if item['op'] == 'ratio' or item['left'] in nullable or item['right'] in nullable: nullable.add(item['id'])
        columns.append({'key': item['id'], 'label': item['label'], 'type': kind})
    raw = ('# Local deterministic compiler; no external model.\nimport json\nimport math\nPLAN = json.loads(' +
           repr(storage._bytes(config).decode('utf-8')) + ')\n' + _PROGRAM).encode('utf-8')
    return raw, columns, nullable


def _boundary(label):
    """Test seam for interruptions at existing shared publication points."""


def _path(path):
    root = Path(os.path.abspath(str(path)))
    storage._safe(root)
    return root


def _decode(payload, columns, nullable, binding, plan):
    storage._keys(payload, 'rows warnings')
    _require(type(payload['rows']) is list and len(payload['rows']) <= (plan['limit'] or 4096) and
             type(payload['warnings']) is list and all(type(v) is str and v in WARNINGS for v in payload['warnings']) and
             payload['warnings'] == sorted(set(payload['warnings'])), 'invalid_output', '执行返回了不符合结果表约定的数据。')
    rows, seen = [], set()
    for raw in payload['rows']:
        _require(type(raw) is dict and set(raw) == {c['key'] for c in columns}, 'invalid_output', '执行返回的结果列不匹配。')
        row = dict(raw)
        for col in columns:
            value = row[col['key']]
            valid = (value is None and col['key'] in nullable or
                     col['type'] == 'string' and type(value) is str or
                     col['type'] == 'integer' and type(value) is int or
                     col['type'] == 'number' and type(value) is float and math.isfinite(value))
            _require(valid, 'invalid_output', '执行返回的结果类型不匹配或包含非有限数值。')
        for index, name in enumerate(plan['group_by']):
            symbol = binding['mapping']['fields'][name]
            if symbol in binding['enums']:
                row['g' + str(index)] = restore_value(raw['g' + str(index)], binding['enums'][symbol], SCOPE)
        group = tuple(row['g' + str(i)] for i in range(len(plan['group_by'])))
        _require(group not in seen, 'invalid_output', '执行返回了重复分组。')
        seen.add(group)
        rows.append(row)
    return {'rows': rows, 'warnings': [WARNINGS[key] for key in payload['warnings']]}


def _observation(record, manifest, binding, plan, columns, nullable):
    storage._keys(record, 'version manifest_sha256 receipt payload payload_sha256')
    _require(type(record['version']) is int and record['version'] == 1 and
             record['manifest_sha256'] == storage._hash(storage._bytes(manifest)) and
             record['payload_sha256'] == storage._hash(storage._bytes(record['payload'])),
             'execution_identity_changed', '执行记录校验失败。')
    storage._receipt(record['receipt'], manifest['runtime'])
    if record['receipt']['status'] != 'success':
        _require(record['payload'] is None, 'invalid_execution_record', '失败记录不能包含可用结果。')
        return 'FAILED', None, record['receipt']['status']
    try:
        return 'SUCCESS', _decode(record['payload'], columns, nullable, binding, plan), None
    except (ValueError, TypeError, KeyError, storage.WorkflowError):
        return 'FAILED', None, 'invalid_table_output'


def _recover_locked(root):
    storage._entries(root, {'writer.lock', 'manifest.json', 'request.json', 'candidate.py', 'binding.json',
                            'spec.json', 'execution.json', 'execution-ref.json', 'ledger'})
    manifest, binding, plan, request = [storage._json(root / name) for name in
                                      ['manifest.json', 'binding.json', 'spec.json', 'request.json']]
    storage._keys(manifest, 'version task run_id runtime candidate_sha256 request_sha256 binding_sha256 spec_sha256 columns')
    _require(type(manifest['version']) is int and manifest['version'] == 1 and manifest['task'] == TASK and
             str(uuid.UUID(manifest['run_id'])) == manifest['run_id'], 'invalid_run_identity', '分析运行身份不匹配。')
    runtime = manifest['runtime']
    _require(runtime == storage.runtime_identity(Limits(**runtime['limits'])), 'runtime_changed', '本地执行环境已改变，保留原记录。')
    for name, value in [('binding', binding), ('spec', plan), ('request', request)]:
        _require(storage._hash(storage._bytes(value)) == manifest[name + '_sha256'], 'input_identity_changed', '保存的输入或方案已改变。')
    schema = public_schema(binding)
    _require(binding['namespace'] == NAMESPACE and binding['scope'] == SCOPE, 'binding_identity_changed', '字段绑定身份不匹配。')
    storage._keys(request, 'records binding_receipt')
    columns_by_symbol = {symbol: name for name, symbol in binding['mapping']['fields'].items()}
    restored = []
    for row in request['records']:
        restored.append({columns_by_symbol[symbol]: restore_value(value, binding['enums'][symbol], SCOPE)
                         if symbol in binding['enums'] else value for symbol, value in row.items()})
    frame = pd.DataFrame(restored).loc[:, list(binding['mapping']['fields'])]
    canonical = validate_spec(frame, plan)
    records, receipt = bind_records(frame, binding, schema, NAMESPACE, SCOPE, expected_binding_digest=binding['digest'])
    _require(records == request['records'] and receipt == request['binding_receipt'], 'input_identity_changed', '输入绑定记录已改变。')
    candidate, columns, nullable = _compile(frame, canonical, binding)
    _require(storage._read(root / 'candidate.py', runtime['limits']['candidate_bytes']) == candidate and
             storage._hash(candidate) == manifest['candidate_sha256'] and columns == manifest['columns'],
             'candidate_identity_changed', '编译程序或结果列定义已改变。')
    record = storage._json(root / 'execution.json') if (root / 'execution.json').exists() else None
    observed, result, reason = _observation(record, manifest, binding, canonical, columns, nullable) if record else (None, None, None)
    status = recover_ledger(root, manifest, candidate, record, observed, error=WorkbenchError, read_only=True)
    return {'status': {'SUCCESS': 'EXECUTED_UNVERIFIED', 'FAILED': 'EXECUTION_FAILED',
                       'UNKNOWN_BLOCKED': 'UNKNOWN_BLOCKED'}.get(status, 'NOT_RUN'),
            'correctness': 'NOT_EVALUATED', 'run_id': manifest['run_id'], 'columns': columns,
            'rows': result['rows'] if status == 'SUCCESS' else [],
            'warnings': result['warnings'] if status == 'SUCCESS' else [],
            'reason': reason if status == 'FAILED' else None, 'candidate_sha256': manifest['candidate_sha256'],
            'binding_digest': binding['digest'], 'source': 'local_deterministic'}


@_guarded
def run_analysis(frame, spec, out_dir, *, limits=None):
    plan = validate_spec(frame, spec)
    root = _path(out_dir)
    _require(not root.exists(), 'run_exists', '本次运行目录已存在；请保留首次记录并创建新运行。')
    limits = limits or Limits(input_bytes=1024 * 1024, result_bytes=1024 * 1024)
    runtime = storage.runtime_identity(limits)
    binding = issue_binding(frame, NAMESPACE, SCOPE)
    records, receipt = bind_records(frame, binding, public_schema(binding), NAMESPACE, SCOPE,
                                   expected_binding_digest=binding['digest'])
    candidate, columns, nullable = _compile(frame, plan, binding)
    request = {'records': records, 'binding_receipt': receipt}
    _require(len(json.dumps(records, allow_nan=False).encode()) <= limits.input_bytes and
             len(candidate) <= limits.candidate_bytes, 'input_size', '输入或编译结果超过本地执行大小上限。')
    try:
        run_id = str(uuid.UUID(root.name))
    except ValueError:
        run_id = str(uuid.uuid4())
    manifest = {'version': 1, 'task': TASK, 'run_id': run_id, 'runtime': runtime,
                'candidate_sha256': storage._hash(candidate), 'columns': columns}
    for name, value in [('request', request), ('binding', binding), ('spec', plan)]:
        manifest[name + '_sha256'] = storage._hash(storage._bytes(value))
    with storage._Writer(root, create=True):
        for name, value in [('request', request), ('binding', binding), ('spec', plan), ('manifest', manifest)]:
            storage._publish(root / (name + '.json'), storage._bytes(value))
        storage._publish(root / 'candidate.py', candidate)
        execute_once(root, manifest, records, candidate,
                     lambda record: _observation(record, manifest, binding, plan, columns, nullable),
                     limits=limits, executor=execute, boundary=_boundary)
        return _recover_locked(root)


@_guarded
def recover_analysis(out_dir):
    """Validate and read a frozen outcome without executing or publishing recovery."""
    root = _path(out_dir)
    with storage._Writer(root):
        return _recover_locked(root)


# Prospective dynamic programs use the same worker and attempt ledger as v1.
AGENT_TASK = 'local-agent-table-v1'


def validate_output_contract(contract, binding):
    _fields(contract, 'columns max_rows', '结果约定')
    columns = contract['columns']
    _require(type(columns) is list and 1 <= len(columns) <= 32 and
             type(contract['max_rows']) is int and 1 <= contract['max_rows'] <= 1000,
             'invalid_output_contract', '结果需要 1～32 列且行数上限为 1～1000。')
    keys = set()
    for col in columns:
        _fields(col, 'key label type nullable restore_from', '结果列')
        _require(type(col['key']) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', col['key'])
                 and col['key'] not in keys and type(col['label']) is str
                 and 1 <= len(col['label']) <= 80 and col['type'] in {'string', 'integer', 'number'}
                 and type(col['nullable']) is bool, 'invalid_output_contract', '结果列定义无效。')
        keys.add(col['key'])
        _require(col['restore_from'] is None or
                 type(col['restore_from']) is str and col['restore_from'] in binding['enums']
                 and col['type'] == 'string', 'invalid_output_contract', '结果还原列必须引用文本角色。')
    return json.loads(storage._bytes(contract))


def _agent_observation(record, manifest, binding, contract):
    storage._keys(record, 'version manifest_sha256 receipt payload payload_sha256')
    _require(type(record['version']) is int and record['version'] == 1 and
             record['manifest_sha256'] == storage._hash(storage._bytes(manifest)) and
             record['payload_sha256'] == storage._hash(storage._bytes(record['payload'])),
             'execution_identity_changed', '执行记录校验失败。')
    storage._receipt(record['receipt'], manifest['runtime'])
    if record['receipt']['status'] != 'success':
        _require(record['payload'] is None, 'invalid_execution_record', '失败记录不能包含结果。')
        return 'FAILED', None, record['receipt']['status']
    try:
        payload = record['payload']
        storage._keys(payload, 'rows')
        _require(type(payload['rows']) is list and len(payload['rows']) <= contract['max_rows'],
                 'invalid_output', '结果行数超出约定。')
        rows = []
        for raw in payload['rows']:
            _require(type(raw) is dict and set(raw) == {c['key'] for c in contract['columns']},
                     'invalid_output', '结果列不匹配。')
            row = dict(raw)
            for col in contract['columns']:
                value = raw[col['key']]
                valid = (value is None and col['nullable'] or
                         col['type'] == 'string' and type(value) is str and len(value) <= 4096 or
                         col['type'] == 'integer' and type(value) is int or
                         col['type'] == 'number' and type(value) in (int, float) and math.isfinite(value))
                _require(valid, 'invalid_output', '结果类型不匹配。')
                if value is not None and col['restore_from'] is not None:
                    if binding.get('kind') == 'published-examples':
                        _require(value in binding['enums'][col['restore_from']], 'invalid_output', 'Public scoped token is not in the examples.')
                    else:
                        row[col['key']] = restore_value(value, binding['enums'][col['restore_from']], SCOPE)
            rows.append(row)
        return 'SUCCESS', rows, None
    except (ValueError, TypeError, KeyError, OverflowError, storage.WorkflowError):
        return 'FAILED', None, 'invalid_table_output'


PUBLIC_AGENT_TASK = 'published-agent-examples-v1'

def _public_binding(schema, examples):
    """Descriptor for exact public rows; never issue or apply private bindings."""
    _fields(schema, 'schema_version fields', 'Public schema')
    fields = schema['fields']
    _require(schema['schema_version']==1 and type(fields) is list and 1<=len(fields)<=32
        and type(examples) is list and 1<=len(examples)<=4096,
        'invalid_public_examples', 'Published examples or schema are invalid.')
    names=[]
    for index,field in enumerate(fields,1):
        _fields(field,'name dtype','Public field')
        _require(field['name']=='COL_{0:04d}'.format(index)
            and field['dtype'] in {'object','int64','float64','bool'},
            'invalid_public_examples','Published field is invalid.')
        names.append(field['name'])
    for row in examples:
        _require(type(row) is dict and set(row)==set(names),
            'invalid_public_examples','Published row columns differ.')
        for field in fields:
            value=row[field['name']];kind=field['dtype']
            valid=(kind=='object' and type(value) is str and 0<len(value)<=4096 or
                kind=='int64' and type(value) is int or
                kind=='float64' and type(value) in (int,float) and math.isfinite(value) or
                kind=='bool' and type(value) is bool)
            _require(valid,'invalid_public_examples','Published value does not match its schema.')
    binding={'kind':'published-examples','schema':schema,'examples':examples,
        'enums':{field['name']:sorted({r[field['name']] for r in examples})
            for field in fields if field['dtype']=='object'}}
    binding['digest']=storage._hash(storage._bytes(binding))
    return binding

def _public_request(binding):
    return {'records':binding['examples'],'binding_receipt':{
        'scope':'published_examples_only','binding_digest':binding['digest'],
        'records_sha256':storage._hash(storage._bytes(binding['examples']))}}

def _recover_agent_locked(root):
    storage._entries(root, {'writer.lock', 'manifest.json', 'request.json', 'candidate.py',
                           'binding.json', 'contract.json', 'execution.json', 'execution-ref.json', 'ledger'})
    manifest, binding, contract, request = [storage._json(root / name) for name in
        ['manifest.json', 'binding.json', 'contract.json', 'request.json']]
    storage._keys(manifest, 'version task run_id runtime candidate_sha256 request_sha256 binding_sha256 contract_sha256 source bundle_sha256'
        + (' owner_id' if manifest.get('task')==PUBLIC_AGENT_TASK else ''))
    _require(manifest['version'] == 1 and manifest['task'] in {AGENT_TASK,PUBLIC_AGENT_TASK} and
             str(uuid.UUID(manifest['run_id'])) == manifest['run_id'],
             'invalid_run_identity', 'Agent 运行身份不匹配。')
    runtime = manifest['runtime']
    _require(runtime == storage.runtime_identity(Limits(**runtime['limits'])),
             'runtime_changed', '本地执行环境已改变。')
    for name, value in [('binding', binding), ('contract', contract), ('request', request)]:
        _require(storage._hash(storage._bytes(value)) == manifest[name + '_sha256'],
                 'input_identity_changed', '已保存的输入约定已改变。')
    if manifest['task']==PUBLIC_AGENT_TASK:
        expected=_public_binding(binding['schema'],binding['examples'])
        _require(binding==expected and request==_public_request(expected),
            'input_identity_changed','Published execution input changed.')
        validate_output_contract(contract,binding)
    else:
        schema = public_schema(binding)
        _require(binding['namespace'] == NAMESPACE and binding['scope'] == SCOPE,
                 'binding_identity_changed', '字段绑定身份不匹配。')
        validate_output_contract(contract, binding)
        storage._keys(request, 'records binding_receipt')
        names = {symbol: name for name, symbol in binding['mapping']['fields'].items()}
        restored = [{names[s]: restore_value(v, binding['enums'][s], SCOPE) if s in binding['enums'] else v
                     for s, v in row.items()} for row in request['records']]
        frame = pd.DataFrame(restored).loc[:, list(binding['mapping']['fields'])]
        records, receipt = bind_records(frame, binding, schema, NAMESPACE, SCOPE,
                                       expected_binding_digest=binding['digest'])
        _require(records == request['records'] and receipt == request['binding_receipt'],
                 'input_identity_changed', '输入绑定记录已改变。')
    candidate = storage._read(root / 'candidate.py', runtime['limits']['candidate_bytes'])
    _require(storage._hash(candidate) == manifest['candidate_sha256'],
             'candidate_identity_changed', '原始候选程序已改变。')
    record = storage._json(root / 'execution.json') if (root / 'execution.json').exists() else None
    observed, rows, reason = _agent_observation(record, manifest, binding, contract) if record else (None, None, None)
    status = recover_ledger(root, manifest, candidate, record, observed, error=WorkbenchError, read_only=True)
    return {'status': {'SUCCESS': 'EXECUTED_UNVERIFIED', 'FAILED': 'EXECUTION_FAILED',
                       'UNKNOWN_BLOCKED': 'UNKNOWN_BLOCKED'}.get(status, 'NOT_RUN'),
            'correctness': 'NOT_EVALUATED', 'run_id': manifest['run_id'],
            'columns': [{k: c[k] for k in ('key', 'label', 'type')} for c in contract['columns']],
            'rows': rows if status == 'SUCCESS' else [], 'warnings': [],
            'reason': reason if status == 'FAILED' else None,
            'candidate_sha256': manifest['candidate_sha256'], 'binding_digest': binding['digest'],
            'bundle_sha256': manifest['bundle_sha256'], 'source': manifest['source']}


def _run_agent_records(candidate, contract, binding, request, out_dir, *,
                       source, bundle_sha256, task, limits=None, owner_id=None):
    contract = validate_output_contract(contract, binding)
    _require(type(candidate) is bytes and candidate and
             source in {'local_test_double', 'qoder', 'external_http', 'codex', 'acp', 'claude', 'remote_node'} and
             type(bundle_sha256) is str and re.fullmatch('[0-9a-f]{64}', bundle_sha256),
             'invalid_candidate', 'Candidate identity is invalid.')
    root = _path(out_dir)
    _require(not root.exists(), 'run_exists', 'Preserve the existing run.')
    limits = limits or Limits(input_bytes=1024 * 1024, result_bytes=1024 * 1024)
    records=request['records']
    _require(len(candidate)<=limits.candidate_bytes and
             len(storage._bytes(records))<=limits.input_bytes,
             'input_size','Input or candidate exceeds the execution bound.')
    manifest={'version':1,'task':task,'run_id':str(uuid.uuid4()),
        'runtime':storage.runtime_identity(limits),'source':source,
        'bundle_sha256':bundle_sha256,'candidate_sha256':storage._hash(candidate)}
    if task==PUBLIC_AGENT_TASK:manifest['owner_id']=str(uuid.UUID(owner_id))
    try: manifest['run_id']=str(uuid.UUID(root.name))
    except ValueError: pass
    for name,value in [('request',request),('binding',binding),('contract',contract)]:
        manifest[name+'_sha256']=storage._hash(storage._bytes(value))
    with storage._Writer(root,create=True):
        for name,value in [('request',request),('binding',binding),('contract',contract),('manifest',manifest)]:
            storage._publish(root/(name+'.json'),storage._bytes(value))
        storage._publish(root/'candidate.py',candidate)
        execute_once(root,manifest,records,candidate,
            lambda record:_agent_observation(record,manifest,binding,contract),
            limits=limits,executor=execute,boundary=_boundary)
        return _recover_agent_locked(root)


@_guarded
def run_candidate(frame, candidate, contract, binding, out_dir, *, source, bundle_sha256, limits=None):
    """Execute original bytes with the existing private synthetic binding."""
    _frame(frame)
    records,receipt=bind_records(frame,binding,public_schema(binding),NAMESPACE,SCOPE,
        expected_binding_digest=binding['digest'])
    return _run_agent_records(candidate,contract,binding,
        {'records':records,'binding_receipt':receipt},out_dir,source=source,
        bundle_sha256=bundle_sha256,task=AGENT_TASK,limits=limits)


@_guarded
def run_public_candidate(candidate, bundle, out_dir, *, source, limits=None, proposal_id=None):
    """Exact published examples through the same supervisor and once-only ledger."""
    binding=_public_binding(bundle['schema'],bundle['examples'])
    return _run_agent_records(candidate,bundle['output'],binding,_public_request(binding),
        out_dir,source=source,bundle_sha256=storage._hash(storage._bytes(bundle)),
        task=PUBLIC_AGENT_TASK,limits=limits,owner_id=proposal_id or str(uuid.uuid4()))


@_guarded
def recover_candidate(out_dir):
    with storage._Writer(_path(out_dir)):
        return _recover_agent_locked(_path(out_dir))
