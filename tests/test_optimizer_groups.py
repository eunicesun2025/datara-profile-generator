import asyncio
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from datara.app import create_app
from datara.domain import ExtractionGroup, FieldDef, new_profile, validate_profile, normalize
from datara.evaluation import grouped_metrics
from datara.generators import render_bound_groups, text_hash, sql, mapping_rows
from datara.optimizer import (create_run_record, import_prompt_version, ensure_prompt_version,
                              promote_version, rollback_version, decide_candidate)
from datara.optimizer_models import CandidateResponse, OptimizationRunCreate
from datara.storage import Store, Conflict


def fixture(store):
    p = new_profile('Cheque joint rules')
    fields = [FieldDef(name=n) for n in ['cheque_number', 'bank_sort_code', 'account_no']]
    p.tables[0].fields[:0] = fields
    normalize(p)
    p = store.save(p)
    rule = '【号码联合规则】先完整读取底部号码，再切分三个字段。'
    base = '你是支票提取助手。只输出原有JSON。\n' + rule + '\n【其他规则】保留前导零。'
    group = ExtractionGroup(name='MICR', field_ids=[f.id for f in fields], source_text=rule, extraction=rule)
    version = import_prompt_version(store, p, base, p.revision, [group])
    return store.load(p.id), fields, group, base, version


def test_bind_preserves_full_prompt_schema_and_rejects_bad_spans(tmp_path):
    store = Store(tmp_path)
    p, fields, group, base, v = fixture(store)
    assert v['rendered_prompt'] == base + '\n'
    assert set(v['grouped_field_ids']) == {f.id for f in fields}
    assert not v['unmapped_field_ids']
    original = p.model_copy(deep=True)
    original.extraction_groups = []
    assert sql(p) == sql(original) and mapping_rows(p) == mapping_rows(original)
    assert not validate_profile(p)['errors']
    for bad in [base + group.source_text, base.replace(group.source_text, '')]:
        with pytest.raises(ValueError, match='唯一'):
            import_prompt_version(store, p, bad, p.revision, [group])
        assert store.load(p.id).revision == p.revision
    mutated = group.model_copy(update={'extraction': '改写原文'})
    with pytest.raises(ValueError, match='只归类'):
        import_prompt_version(store, p, base, p.revision, [mutated])
    duplicate = group.model_copy(update={'id': 'another'})
    with pytest.raises(ValueError):
        import_prompt_version(store, p, base, p.revision, [group, duplicate])


def test_group_candidate_run_promotion_and_rollback(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path)) as client:
        store = client.app.state.store
        p, fields, group, base, version = fixture(store)
        image = io.BytesIO();Image.new('RGB', (30, 30)).save(image, 'PNG')
        sample = client.post('/api/samples', files={'file': ('sample.png', image.getvalue())}).json()
        expected = {p.tables[0].name: dict(zip([f.name for f in fields], ['000123', '024', '123456']))}
        case = client.post(f'/api/optimizer/profiles/{p.id}/test-cases', json={
            'sample_id': sample['id'], 'name': 'cheque', 'dataset_role': 'failure',
            'ground_truth': expected}).json()
        run = create_run_record(store, OptimizationRunCreate(profile_id=p.id, profile_revision=p.revision,
            selected_field_ids=[fields[2].id], failure_test_case_ids=[case['id']]))
        assert len(run['selected_fields']) == 3 and len(run['selected_groups']) == 1
        service = client.app.state.optimizer_service
        new_rule = group.extraction + '分别计数，符号不是数字，不吞掉重复数字。'
        response = CandidateResponse(candidate_group_rules=[dict(group_id=group.id,
            old_rule_hash=text_hash(group.extraction), new_rule=new_rule, reason='解决联合边界')])
        candidate, diffs = service._build_candidate_version(run, version, response, '{}', 'iteration')
        assert candidate['rendered_prompt'] == base.replace(group.extraction, new_rule) + '\n'
        assert candidate['field_rules'] == version['field_rules']
        assert diffs[0]['field_ids'] == group.field_ids
        bad = CandidateResponse(candidate_rules=[dict(field_id=fields[0].id,
            old_rule_hash=text_hash(''), new_rule='不允许单独覆盖', reason='bad')])
        with pytest.raises(ValueError, match='成员不能'):
            service._build_candidate_version(run, version, bad, '{}', 'bad')
        response.candidate_group_rules[0].old_rule_hash = '0'*64
        with pytest.raises(Conflict):
            service._build_candidate_version(run, version, response, '{}', 'stale')

        async def fake(connection, key, instructions, images, **kwargs):
            if 'candidate_group_rules' in instructions:
                return json.dumps({'candidate_group_rules': [dict(group_id=group.id,
                    old_rule_hash=text_hash(group.extraction), new_rule=new_rule, reason='联合读取')]})
            result = json.loads(json.dumps(expected))
            if new_rule not in instructions:
                result[p.tables[0].name]['account_no'] = '1123456'
            return json.dumps(result)
        monkeypatch.setattr('datara.optimizer.completion', fake)
        asyncio.run(service.run(run['id']))
        finished = store.read_json('optimizer/runs', run['id'])
        assert finished['status'] == 'completed', finished
        assert finished['promotion_eligible']
        assert finished['baseline_metrics']['failure_targets']['ratio'] == 0
        assert finished['final_validation_metrics']['failure_targets']['ratio'] == 1
        saved, promoted = promote_version(store, finished['best_version_id'], p.revision, version['id'], run['id'])
        assert saved.extraction_groups[0].extraction == new_rule
        assert ensure_prompt_version(store, saved)['id'] == promoted['id']
        restored, rolled = rollback_version(store, version['id'], saved.revision, promoted['id'])
        assert restored.extraction_groups[0].extraction == group.extraction
        assert rolled['rendered_prompt'] == base + '\n'


def test_group_metric_does_not_reward_trading_member_errors():
    group = {'id': 'g', 'field_ids': ['a', 'b', 'c']}
    def rows(matches):
        return [dict(test_case_id='one', repetition=1, field_id=k, selected=True,
                     matched=k in matches, structure_valid=True) for k in group['field_ids']]
    a = grouped_metrics(rows(['a']), [], [group])
    b = grouped_metrics(rows(['a', 'b']), [], [group])
    assert a['failure_targets']['ratio'] == b['failure_targets']['ratio'] == 0
    assert grouped_metrics(rows(['a','b','c']), [], [group])['failure_targets']['ratio'] == 1
    assert grouped_metrics(rows(['a','b','c'])[:2], [], [group])['failure_targets']['ratio'] == 0


def test_binding_does_not_absorb_group_into_preceding_field(tmp_path):
    store = Store(tmp_path)
    p = new_profile('Import mixed prompt')
    fields = [FieldDef(name=n) for n in ['customer_name', 'cheque_number', 'bank_sort_code', 'account_no']]
    p.tables[0].fields[:0] = fields
    normalize(p);p=store.save(p)
    block='【三个号码必须联合提取】\n整行读取，然后划分三个字段。'
    base='你是提取助手。\ncustomer_name: 提取出票人名称。\n\n'+block+'\nJSON 输出要求:\n只返回 JSON。'
    group=ExtractionGroup(name='MICR',field_ids=[f.id for f in fields[1:]],source_text=block,extraction=block)
    version=import_prompt_version(store,p,base,p.revision,[group])
    saved=store.load(p.id)
    assert saved.tables[0].fields[0].extraction == '提取出票人名称。'
    assert version['rendered_prompt'].strip() == base


def test_bound_blocks_replace_simultaneously_without_replacing_new_text():
    p=new_profile('two groups')
    p.extraction_groups=[ExtractionGroup(id='a',name='a',field_ids=['a','b'],source_text='original A',extraction='original B'),
                         ExtractionGroup(id='b',name='b',field_ids=['c','d'],source_text='original B',extraction='new B')]
    assert render_bound_groups('before original A between original B after',p) == 'before original B between new B after'
    p.extraction_groups[1].source_text='A between original B'
    with pytest.raises(ValueError,match='重叠'):
        render_bound_groups('before original A between original B after',p)


def test_mixed_target_gain_cannot_hide_group_regression():
    def score(x):return {'ratio':x}
    best={'failure_selected':score(.5),'failure_targets':score(.5),'regression_all':score(1),
          'failure_groups':{'g':score(1)},'regression_groups':{'g':score(1)}}
    candidate={**best,'failure_targets':score(.8),'failure_groups':{'g':score(0)}}
    accepted,reasons=decide_candidate(best,best,candidate,{'minimum_improvement':.01,'max_regression_drop':0})
    assert not accepted and any('联合字段组' in r for r in reasons)
