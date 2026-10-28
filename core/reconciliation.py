"""Offline usage-to-statement comparison. Never estimates prices or changes balances."""
from __future__ import annotations
import argparse,json
from datetime import datetime
from pathlib import Path

def reconcile(ledger_rows,statement):
    if not isinstance(statement,dict) or statement.get('schema_version')!=1:
        raise ValueError('供应商账单需要 schema_version=1')
    start=datetime.fromisoformat(statement['window_start']);end=datetime.fromisoformat(statement['window_end'])
    if start.tzinfo is None or end.tzinfo is None or end<=start:raise ValueError('账单窗口必须带时区且结束晚于开始')
    actual={};missing=0;observed=0
    for row in ledger_rows:
        try:
            at=datetime.fromisoformat(str(row.get('ts','')))
            if at.tzinfo is None:at=at.replace(tzinfo=start.tzinfo)
            if not start<=at<end:continue
            observed+=1
            if row.get('usage_source')!='host_reported':missing+=1
            model=str(row.get('model') or 'unknown');actual[model]=actual.get(model,0)+max(0,int(row.get('tokens',0)))
        except (ValueError,TypeError):continue
    declared={}
    for row in statement.get('models',[]):
        model=str(row['model']);declared[model]=declared.get(model,0)+max(0,int(row['tokens']))
    differences=[{'model':model,'plugin_tokens':actual.get(model,0),'statement_tokens':declared.get(model,0),
                  'difference':actual.get(model,0)-declared.get(model,0)} for model in sorted(actual.keys()|declared.keys())]
    matched=bool(observed) and not missing and all(row['difference']==0 for row in differences)
    return {'window_start':start.isoformat(),'window_end':end.isoformat(),'granularity':'model_token_totals',
            'status':'usage_matched' if matched else 'insufficient_usage' if missing or not observed else 'difference',
            'missing_usage_calls':missing,'differences':differences,'cost_verified':False,
            'note':'仅核对同窗口模型 token 汇总；账单金额、折扣、税费及实际扣费需另有原始凭证。'}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--ledger',required=True);parser.add_argument('--statement',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    rows=[json.loads(line) for line in Path(args.ledger).read_text(encoding='utf-8').splitlines() if line.strip()]
    report=reconcile(rows,json.loads(Path(args.statement).read_text(encoding='utf-8')))
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
