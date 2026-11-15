"""所有生成共用的有界重试、输出警告与模板回退。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import random
import time
from typing import Any, Callable
from uuid import uuid4

from .generation_evidence import request_scope
from .llm_templates import (FormatFailure, SemanticFailure, THRESHOLDS,
                            definition, fallback, validate)


class GeneratedText(str):
    """保持字符串接口，同时让消费方辨认模板来源。"""
    def __new__(cls,result):
        value=super().__new__(cls,result.text)
        value.result=result
        value.used_template=result.used_template
        value.template_name=result.template_name
        value.fallback_reason=result.fallback_reason
        return value


class GenerationPolicy:
    def __init__(self,emit:Callable[...,Any]|None=None,*,sleep=None,rng=None):
        self._emit=emit
        self._sleep=sleep or asyncio.sleep
        self._rng=rng or random.Random()

    def _record(self,**entry):
        if self._emit is not None:
            try:self._emit(**entry)
            except Exception:pass

    async def run(self,invoke,*,event,module,task_name='',validator=None,
                  fallback_context=None,tier=None,light=None,timeout_ms=None,
                  max_attempts=None):
        spec=definition(event)
        tier=tier or spec.get('tier','small')
        if tier not in THRESHOLDS:tier='small'
        threshold=THRESHOLDS.get(tier,4000)
        light=bool(spec.get('light',tier=='small') if light is None else light)
        retry_limit=1 if light else 3
        # The forced format repair has one dedicated attempt; lightweight tasks share
        # their single retry across every failure category.
        total_limit=2 if light else 5
        if max_attempts is not None:total_limit=max(1,min(total_limit,int(max_attempts)))
        deadline=time.monotonic()+timeout_ms/1000 if timeout_ms else None
        group_id=uuid4().hex
        results=[];standard_retries=0;forced=False
        next_limit=None;route=task_name;failure='';last=None
        while len(results)<total_limit:
            remaining=None if deadline is None else max(0,deadline-time.monotonic())
            if remaining is not None and remaining<.02:
                failure='生成等待已到期';break
            scope={'group_id':group_id,'attempt':len(results)+1}
            scope_token=request_scope.set(scope)
            try:
                operation=invoke(max_tokens=next_limit,timeout_ms=int(remaining*1000) if remaining is not None else timeout_ms,
                                 attempt=len(results),task_name=route)
                result=await asyncio.wait_for(operation,remaining) if remaining is not None else await operation
            except asyncio.CancelledError:
                self._record(event=event,module=module,task=route,level='error',message='生成已取消',
                             category='cancelled',group_id=group_id,request_id=scope.get('request_id',''),
                             attempt=len(results)+1,tier=tier,warning_threshold=threshold)
                raise
            # The invocation adapter converts host errors to LLMResult. A timeout
            # around the whole job also must resolve to a template, not escape it.
            except (asyncio.TimeoutError,TimeoutError):
                from .llm_utils import LLMResult
                failure='生成等待已到期'
                result=LLMResult(False,'','timeout',error=failure,module=module,event=event,
                                 task_name=route,request_id=scope.get('request_id',''),request_sent=True,consumed=True,attempts=1)
            except Exception:
                self._record(event=event,module=module,task=route,level='error',message='生成执行异常',
                             category='exception',group_id=group_id,request_id=scope.get('request_id',''),
                             attempt=len(results)+1,tier=tier,warning_threshold=threshold)
                raise
            finally:
                request_scope.reset(scope_token)
            last=result;results.append(result)
            error_kind='';failure=''
            if result.ok:
                try:
                    if result.finish_reason in ('length','max_tokens'):
                        raise FormatFailure('模型输出被截断')
                    validate(result.text,event)
                    if validator is not None:
                        outcome=validator(result.text)
                        if outcome is False or outcome is None:raise SemanticFailure('结果未通过任务校验')
                except FormatFailure as exc:error_kind='format';failure=str(exc)
                except SemanticFailure as exc:error_kind='semantic';failure=str(exc)
                except Exception as exc:error_kind='semantic';failure='任务校验未通过：'+type(exc).__name__
            else:
                failure={'timeout':'模型调用超时','task_missing':'模型任务路由不可用',
                         'empty':'模型没有返回正文'}.get(result.category,'模型调用失败')
                error_kind='format' if result.category=='empty' or result.finish_reason in ('length','max_tokens') else 'transport'
            completion=result.completion_tokens
            over=isinstance(completion,int) and not isinstance(completion,bool) and completion>threshold
            level='error' if error_kind else 'warn' if over else 'info'
            self._record(event=event,module=module,task=result.task_name or route,model=result.model,
                         level=level,message=failure if error_kind else '输出超过预期消耗' if over else '生成完成',
                         category=error_kind or result.category,group_id=group_id,request_id=result.request_id,
                         tier=tier,warning_threshold=threshold,completion_tokens=completion,
                         over_threshold=over,attempt=len(results),max_tokens=next_limit,
                         used_template=False)
            if not error_kind:
                answer=self._aggregate(result,results)
                if len(results)>1 and answer.completion_tokens is not None and answer.completion_tokens>threshold:
                    level='warn'
                    self._record(event=event,module=module,task=route,level='warn',message='本次生成累计输出超过预期消耗',
                                 category='generation_summary',group_id=group_id,tier=tier,warning_threshold=threshold,
                                 completion_tokens=answer.completion_tokens,over_threshold=True,attempt=len(results))
                return replace(answer,level=level,tier=tier,warning_threshold=threshold,
                               policy_group_id=group_id)
            # A capped repair is final even if its remaining problem is semantic.
            if forced or len(results)>=total_limit:break
            if error_kind=='format':
                forced=True;next_limit=threshold
            else:
                if error_kind=='transport' and self._permanent(result):break
                if standard_retries>=retry_limit:break
                standard_retries+=1;next_limit=None
                if error_kind=='transport' and result.category=='task_missing':
                    if route=='utils':break
                    route='utils'
            if light and len(results)>=2:break
            delay=min(4.0,.5*(2**(len(results)-1)))*self._rng.uniform(.8,1.2)
            if deadline is not None and time.monotonic()+delay>=deadline:
                failure='生成等待已到期';break
            await self._sleep(delay)
        if last is None:
            # An invocation that consumed the entire timeout is still a failed call.
            from .llm_utils import LLMResult
            last=LLMResult(False,'','timeout',error=failure,module=module,event=event,task_name=route)
        template=fallback(event,fallback_context)
        self._record(event=event,module=module,task=route,level='warn',message='已使用'+template['title'],
                     category='template',group_id=group_id,request_id=last.request_id,tier=tier,warning_threshold=threshold,
                     used_template=True,template_name=template['name'],template_title=template['title'],
                     fallback_reason=failure or '生成未完成',attempt=len(results),completion_tokens=None)
        answer=self._aggregate(last,results)
        return replace(answer,ok=template['ok'],text=template['text'],category='template',
                       used_template=True,template_name=template['name'],template_title=template['title'],
                       fallback_reason=failure or '生成未完成',level='warn',tier=tier,
                       warning_threshold=threshold,policy_group_id=group_id)

    @staticmethod
    def _aggregate(last,results):
        updates={'attempts':sum(max(0,int(result.attempts or 0)) for result in results),
                 'request_sent':any(result.request_sent for result in results),
                 'consumed':any(result.consumed for result in results)}
        for field in ('prompt_tokens','completion_tokens','total_tokens','cache_hit_tokens','cache_miss_tokens'):
            values=[getattr(result,field,None) for result in results]
            updates[field]=sum(values) if values and all(isinstance(value,int) for value in values) else None
        updates['usage_available']=bool(results) and all(result.usage_available for result in results)
        updates['duration_ms']=sum(result.duration_ms for result in results)
        return replace(last,**updates)

    @staticmethod
    def _permanent(result):
        message=str(result.error or '').lower()
        return any(code in message for code in ('invalid_model_request','模型已关闭','unauthorized','invalid api key','authentication failed','content policy'))
