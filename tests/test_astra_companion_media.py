"""Companion, video and Qzone regression tests. Each case runs in an isolated process."""
import sys
sys.dont_write_bytecode = True

import subprocess
import unittest
import asyncio
import json
import logging
import os
import random
import socket
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

ROOT = Path(os.environ.get('CATEYE_TEST_DIR') or Path(__file__).resolve().parents[1]).resolve()


def bootstrap():
    global ShareLedger, VideoSocialService, ManualVideoShares, boosted_limits, AudienceProfiles
    global VideoWatchCoordinator, SurfQuotaLedger, WatchTaskManager, AffectionManager, AffectionReviewer
    global CompanionComponentsMixin, CompanionServices, PatrolService, QzoneService, QzoneComponentsMixin
    sys.path.insert(0, str(ROOT))


    def blocked_network(*args, **kwargs):
        raise AssertionError("Offline probe attempted transport")


    # Leaf-module imports only. Never load plugin.py, start a worker, or bind to any service.
    for name in ("companion", "planning", "video_watch", "core", "qzone", "integrations", "linkage"):
        pkg = ModuleType(name)
        pkg.__path__ = [str(ROOT / name)]
        sys.modules[name] = pkg

    from pydantic import BaseModel, Field
    sdk = ModuleType("maibot_sdk")
    sdk.PluginConfigBase = BaseModel
    sdk.Field = Field
    sdk.API = sdk.Tool = sdk.HookHandler = lambda *args, **kwargs: lambda method: method
    sdktypes = ModuleType("maibot_sdk.types")
    sdktypes.HookMode = NS(OBSERVE="observe", BLOCKING="blocking")
    sdktypes.HookOrder = NS(LATE=1, NORMAL=0, EARLY=-1)
    sdktypes.ErrorPolicy = NS(SKIP="skip")
    sys.modules["maibot_sdk"] = sdk
    sys.modules["maibot_sdk.types"] = sdktypes
    sys.modules["linkage"].get_adapter = lambda *args: None

    from integrations.video_share_ledger import ShareLedger
    from integrations.video_share_scheduler import VideoSocialService, ManualVideoShares, boosted_limits
    from integrations.video_audience import AudienceProfiles
    from integrations.video_schedule import VideoWatchCoordinator, SurfQuotaLedger
    from video_watch.manager import WatchTaskManager
    from companion.affection import AffectionManager
    from companion.affection_referee import AffectionReviewer
    from companion.components import CompanionComponentsMixin
    from companion.services import CompanionServices
    from companion.patrol import PatrolService
    from qzone.service import QzoneService
    from qzone.components import QzoneComponentsMixin

    socket.socket.connect = blocked_network
    socket.socket.connect_ex = blocked_network
    socket.create_connection = blocked_network


A = "BV0000000001"
B = "BV0000000002"
NOW = 1730116800.0
DAY = "2024-10-28"
ROUTE = {"stream_id": "synthetic-stream", "kind": "private", "user_id": "10001", "account_id": "20001"}
CFG = NS(mode="自动分享", candidate_ttl_hours=24, daily_limit=3, stream_daily_limit=1,
         min_interval_hours=6, max_targets_per_video=1)


def observed(ledger, bvid, now):
    ledger.observe({"bvid": bvid, "title": bvid}, now, 86400)


def fixtures(folder, now=NOW, cfg=CFG):
    ledger = ShareLedger(folder / "shares.sqlite")
    sr = {**ROUTE, "fingerprint": "fp", "recent_rate": 0, "last_bot_at": 0}
    profiles = NS(companion_target=lambda: ROUTE,
                  routes=AsyncMock(return_value=[ROUTE]), collect=AsyncMock(return_value=[sr]),
                  update_topics=lambda *a: None)
    social = VideoSocialService(ledger, profiles, NS(evaluate=AsyncMock(return_value={
            "action": "share", "target_stream_id": ROUTE["stream_id"], "profile_topics": {}})),
        config_provider=lambda: cfg, clock=lambda: now, day=lambda: DAY,
        prepare_card=AsyncMock(return_value={"success": True, "ark_json": "{}"}),
        send_card=AsyncMock(return_value={"success": True}),
        preflight=lambda *a: (True, ""), reserve=lambda *a: "", commit=lambda *a: True,
        release=lambda *a: True, append_context=AsyncMock(return_value={"success": True}),
        probability_provider=lambda: 2, roll=lambda: 0)
    return ledger, social


async def share_limits(folder):
    ledger, social = fixtures(folder)
    # At 7h the base global 6h interval has passed, but one previous share occupies the per-stream cap.
    old = NOW - 7 * 3600
    for bvid in [A, B]:
        observed(ledger, bvid, old if bvid == A else NOW)
        if bvid == A:
            ledger.decide(A, {"action": "share", "target_stream_id": ROUTE["stream_id"]}, old)
            c = ledger.claim(A, ROUTE["stream_id"], "auto", "prior", old, DAY, social._limits())
            ledger.finish(c["attempt_id"], "accepted")
    wanted = ledger.capacity_reason(NOW, DAY, boosted_limits(social._limits(), 2), bvid=B, stream=ROUTE["stream_id"])
    observed_result = await social.tick()
    assert wanted == "" and observed_result["status"] == "accepted", observed_result
    assert social._send.await_count == 1


async def wrong_manual_receipt(folder):
    ledger, social = fixtures(folder)
    manual = ManualVideoShares(social, recent_provider=lambda: [{"bvid": k, "title": k} for k in (A, B)], clock=lambda: NOW, day=lambda: DAY)
    a = await manual.share(ROUTE["stream_id"], "user-request-A", video=A)
    b = await manual.share(ROUTE["stream_id"], "user-request-B", video=B)
    assert a["success"] and not b["success"] and b.get("error_class") == "stream_cooldown" and b.get("bvid") == B, b
    assert social._send.await_count == 1


async def profile_count(folder):
    ledger = ShareLedger(folder / "profile.sqlite")
    cfg = NS(allowed_chats=[], allow_all_groups=True, min_group_daily_messages=100, profile_refresh_minutes=5)
    # Well-spaced synthetic rows within the same business day; no sender text or ID from a real chat.
    msgs = [{"message_id": str(i), "time": NOW - 1200 + 4*i, "text": "sample", "user_id": "20005"} for i in range(120)]
    async def read(sid, start, end, limit, limit_mode, **opts):
        found = [m for m in msgs if float(start) <= m["time"] <= float(end)]
        return found[-limit:] if limit_mode == "latest" else found[:limit]
    profiles = AudienceProfiles(ledger,
        stream_reader=AsyncMock(return_value=[{"stream_id": "g", "group_id": "30001", "account_id": "40001"}]),
        message_reader=read, config_provider=lambda: cfg, target_provider=lambda: {})
    first = await profiles.collect(NOW)
    second = await profiles.collect(NOW + 360)
    cached = ledger.profile("g")
    assert len(first) == len(second) == 1 and cached["day_non_bot_count"] >= 100, cached


async def interrupted_eval(folder):
    path = folder / "eval.sqlite"
    ledger = ShareLedger(path)
    observed(ledger, A, NOW)
    observed(ledger, B, NOW + 1)
    for i in range(3):
        stamp = NOW + 600*i
        assert ledger.begin_evaluation(A, stamp)
        if i < 2:
            ledger.decide(A, {"action": "defer", "delay_seconds": 300}, stamp)
    ledger = ShareLedger(path)
    stamp = NOW + 5000
    row = ledger.due(stamp)
    assert row['bvid'] == B and ledger.begin_evaluation(B, stamp), row


async def cancel_cache(folder):
    mgr = WatchTaskManager(folder)
    # Explicitly suppress scheduling a real worker. _finish itself runs the production two-phase write.
    mgr._kick = lambda: None
    submitted = mgr.submit(A, owner="tool")
    ident = submitted["task_id"]
    task = mgr._store.get_task(ident)
    task.update(status="running", started_at=time.time())
    mgr._store.upsert_task(task)
    entered = asyncio.Event()
    async def wait_for_interrupt(result):
        entered.set()
        await asyncio.Future()
    mgr.set_impression_hook(wait_for_interrupt)
    result = {"status":"completed", "video":{"bvid":A,"title":"synthetic title"}, "finished_at":time.time(),
              "content_description": "synthetic description", "subtitle_text": "sample" * 10}
    pending = asyncio.create_task(mgr._finish(task, result))
    await entered.wait()
    mgr.cancel(ident, force=True)
    await pending
    again = mgr.submit(A, owner="tool")
    state = mgr.status(ident)["status"]
    history = mgr._store.load_history()
    assert state == "cancelled" and not history and not again["cached"] and again["status"] == "queued", again


async def gap_quota(folder):
    async def api(action, **kwargs):
        if action == "status":
            return {"success": True, "status":"completed", "started_at":NOW - 120}
        if action == "result":
            return {"success":True,"result":{"status":"completed","finished_at":NOW,"started_at":NOW-120}}
        raise AssertionError(action)
    quota = SurfQuotaLedger(folder, day_provider=lambda:DAY,clock=lambda:NOW)
    coord = VideoWatchCoordinator(folder, api, lambda: {}, lambda _: {}, quota=quota,
                                 clock=lambda: NOW, rng=random.Random(10), config_provider=lambda: NS(enabled=True))
    eid = "gap-synthetic"
    assert quota.reserve_once(eid, "gap", 10)
    coord._save_links({"links":{eid:{"episode_id":eid,"scenario":"gap","task_id":"bw-synthetic","stage":"running",
                                       "reserved_minutes":10,"session_target_minutes":10,"started_at":NOW-120}}})
    with patch('integrations.video_schedule.time.time', return_value=NOW):
        await coord.sync()
    entry = coord.episode_state(eid)
    claim = json.loads((folder/"surf_quota.json").read_text(encoding="utf-8"))["claims"][eid]
    assert entry['watched_minutes_accumulated'] == 2 and claim['state'] == 'settled' and quota.snapshot()['minutes_used'] == 2, claim


async def identity_binding(folder):
    # Directly exercise only local configuration binding and prompt formatting. No SDK, model or outgoing transport.
    affinity = AffectionManager(folder)
    config = NS(patrol=NS(enabled=True), prompts=NS())
    patrol = PatrolService.__new__(PatrolService)
    bundle = CompanionServices.__new__(CompanionServices)
    bundle.patrol, bundle.affection, bundle.relation = patrol, affinity, None
    CompanionServices.set_target(bundle, "10001", "private-A")
    affinity.append_impressions([{"text":"A 的专属测试词——青色纸风车"}])
    affinity.flush()
    CompanionServices.set_target(bundle, "10002", "private-B")
    host = CompanionComponentsMixin.__new__(CompanionComponentsMixin)
    host.config = config
    host._mai_companion = lambda: bundle
    host._mai_lover_name = lambda:"合成角色"
    host._mai_target_qq = lambda:"10002"
    host._mai_target_stream_id = lambda:"private-B"
    scope = host._mai_resolve_injection_scope("private-B", [])
    prompt = host._mai_build_affection_suffix(scope)
    assert "10002" in prompt and "青色纸风车" not in prompt, prompt
    CompanionServices.set_target(bundle, "10001", "private-A2")
    assert affinity.impressions()[-1]["text"].endswith("青色纸风车")


async def review_concurrency(folder):
    affinity = AffectionManager(folder)
    plugin = NS(config=NS(affection=NS(enabled=True, review_enabled=True), patrol=NS(enabled=True)))
    affinity.sync_daily()
    affinity.append_event_entry({"kind":"chat_round", "desc":"before review"})
    affinity.bump_cycle_rounds()
    entered, resume = asyncio.Event(), asyncio.Event()
    captured = []
    async def judge(*args, **kw):
        captured.extend(kw["event_log"])
        entered.set()
        await resume.wait()
        return {"delta_score": 0, "impressions": [], "tone":"平静", "reason":"合成样本"}
    reviewer = AffectionReviewer(plugin, affinity, llm=NS())
    with patch("companion.affection_referee.run_daily_review", judge):
        task = asyncio.create_task(reviewer.perform_daily_review())
        await entered.wait()
        affinity.append_event_entry({"kind":"chat_round", "desc":"after snapshot"})
        affinity.bump_cycle_rounds()
        resume.set()
        result = await task
    assert result["ok"] and len(captured) == 1 and [r["desc"] for r in affinity.event_log()] == ["after snapshot"] and affinity.cycle_rounds() == 1


async def qzone_capacity(folder):
    from integrations.qzone_video_bridge import CANDIDATE_CAPACITY, QzoneVideoLedger
    path = folder / "qzone.sqlite"
    ledger = QzoneVideoLedger(path)
    for i in range(CANDIDATE_CAPACITY):
        assert ledger.observe({"bvid": "BV"+f"{i:010d}", "title":"synthetic"}, "t"+str(i), "e", NOW, 60)
    expired = ledger.expire(NOW+120)
    ledger = QzoneVideoLedger(path)
    accepted = ledger.observe({"bvid":"BV9999999999", "title":"new"}, "new-task", "e", NOW+365*86400, 60)
    assert expired == 512 and accepted


async def qzone_write_timeout(folder):
    config = NS(qzone=NS(), schedule=NS(enabled=True))
    # The fake adapter records an invocation and models an acknowledgement lost after dispatch.
    calls = []
    async def ambiguous(api_name, **kwargs):
        calls.append(api_name)
        raise TimeoutError("synthetic lost acknowledgement")
    client = NS(uin="20001", comment=AsyncMock(return_value=True))
    svc = QzoneService(NS(api=NS(call=ambiguous)),config,NS(),NS(),
                       cookie_manager=NS(get_client=AsyncMock(return_value=client)))
    result = await svc.comment_moment("synthetic-post","合成普通评论")
    assert len(calls) == 1 and client.comment.await_count == 0 and result is None, calls


async def qzone_limit_race(folder):
    class Noon(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 4, 12, 0)

    config = NS(qzone=NS(enabled=True, daily_max_posts=1, post_cooldown_minutes=0,
                         post_llm_gate_enabled=False), schedule=NS(enabled=True))
    svc = QzoneService(NS(), config, NS(), NS())
    svc.bind_state_dir(folder)
    host = QzoneComponentsMixin()
    host.config, host._qzone = config, svc
    arrived = 0
    together = asyncio.Event()

    async def collect_images(*args):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            together.set()
        await together.wait()
        return []

    svc.collect_moment_images = collect_images
    svc._kick_post_media_refresh = lambda: None
    svc.call_action = AsyncMock(side_effect=[(True, {"tid":"first"}, ""), (True, {"tid":"second"}, "")])
    with patch("qzone.service.datetime", Noon), patch("qzone.service._sleep_silence_window", return_value=("23:00", "07:00")):
        outcomes = await asyncio.gather(host.qzone_tool_life("post","A"), host.qzone_tool_life("post","B"))
        assert sum(o["success"] for o in outcomes) == 1 and svc.posts_today() == 1, outcomes
        assert svc.call_action.await_count == 1


async def qzone_activity_toggle(folder):
    config = NS(qzone=NS(require_activity=False,post_rate=1,post_llm_gate_enabled=False),
                schedule=NS(enabled=True))
    svc = QzoneService(NS(),config,NS(find_current_activity=lambda *a,**kw:None),NS())
    svc.generate_moment_text = AsyncMock(return_value="free")
    svc.post_moment = AsyncMock(return_value={"tid":"synthetic"})
    svc.collect_moment_images = AsyncMock(return_value=[])
    with patch("qzone.service._sleep_silence_window",return_value=("23:00","07:00")):
        sent = await svc.generate_and_post_by_schedule("2024-01-01", datetime(2024,1,1,12),post_rate=1)
    hist = svc._decision_history.last()
    assert sent and svc.generate_moment_text.await_count == 1, hist


async def watch_debit_atomicity(folder):
    from integrations import video_schedule
    quota = SurfQuotaLedger(folder, day_provider=lambda:DAY, clock=lambda:NOW)
    quota.snapshot()
    write = video_schedule._atomic_write
    eid = "episode"
    def fail_on_claim(path, document):
        if eid in document.get("claims",{}):
            raise OSError("synthetic write failure")
        write(path, document)
    with patch.object(video_schedule,"_atomic_write",fail_on_claim):
        with unittest.TestCase().assertRaises(OSError):
            quota.reserve_once(eid,"dedicated",10)
    first = quota.snapshot()
    fresh = SurfQuotaLedger(folder,day_provider=lambda:DAY,clock=lambda:NOW)
    retried = fresh.reserve_once(eid,"dedicated",10)
    second = fresh.snapshot()
    assert first["sessions_used"] == 0 and not first.get("claims") and retried and second["sessions_used"] == 1, (first,second)


async def fallback_review(folder):
    from core.llm_policy import GeneratedText
    from core.llm_types import LLMResult
    data = json.loads((ROOT/"mappings/default_mappings/templates/affection_daily_review.json").read_text(encoding="utf-8"))
    model = GeneratedText(LLMResult(True,json.dumps(data["response"]),"template",used_template=True,
                                    template_name=data["event"], fallback_reason="timeout"))
    affinity = AffectionManager(folder)
    plugin = NS(config=NS(affection=NS(enabled=True,review_enabled=True),patrol=NS(enabled=True)))
    affinity.bump_cycle_rounds()
    affinity.append_event_entry({"kind":"chat_round","desc":"waiting for real evaluation"})
    reviewer = AffectionReviewer(plugin, affinity, llm=NS(generate=AsyncMock(return_value=model)))
    out = await reviewer.perform_daily_review(date="2024-01-15")
    assert not out["ok"] and len(affinity.event_log()) == 1 and affinity.review_state()["daily_reviewed_date"] != "2024-01-15", out


async def candidate_overlap(folder):
    from integrations.video_interest import BiliCandidateSource
    doc = {"keywords":["弦乐"],"generation":"g","status":"ready"}
    interest = NS(ensure_interest=AsyncMock(return_value=doc),load=lambda:doc,effective_weights=lambda:{})
    popular = {"code":0,"data":{"list":[{"bvid":A,"title":"作品一号","duration":120,"tname":"乐器",
                                        "stat":{"view":90000,"coin":80}}]}}
    search = {"code":0,"data":{"result":[{"bvid":A,"title":"作品一号","duration":"2:00","typename":"乐器","play":90000}]}}
    async def test_search_only(empty):
        async def fetch(url):
            return search if "search" in url else ({"code":0,"data":{"list":[]}} if empty else popular)
        src = BiliCandidateSource(interest,fetch_json=fetch,selection_path=folder/("search.json" if empty else "both.json"))
        return await src.prepare()
    only = await test_search_only(True)
    both = await test_search_only(False)
    assert only["pool_size"] == both["pool_size"] == 1, both


async def tiered_miss(folder):
    from companion.initiative_profile import InitiativeMemory
    from integrations.optional_action import OptionalActionCoordinator
    anchor = datetime(2024,3,1,8).timestamp()
    epoch = [anchor+10*3600]
    class Clock(datetime):
        @classmethod
        def now(cls,*args,**kw):return datetime.fromtimestamp(epoch[0])
    patrol = PatrolService.__new__(PatrolService)
    patrol._target_qq,patrol._stream_id = "10001","private-test"
    patrol._last_miss_claim_kind = ""
    patrol._affection = NS(today_date=lambda:Clock.now().strftime("%Y-%m-%d"),
        last_user_msg_time=lambda:datetime.fromtimestamp(anchor), set_miss_sent=lambda:None)
    mem = InitiativeMemory(folder)
    mem.cycle("10001",anchor,anchor)
    patrol._initiative_memory=mem
    permits = lambda:{"enabled":True,"target_stream":"private-test","coverage":"ok","budget_remaining":5}
    trigger = AsyncMock(return_value={"success":True})
    budget = NS(reserve=lambda *a:"synthetic-token", commit_reservation=lambda *a:True,release_reservation=lambda *a:True)
    coordinator = OptionalActionCoordinator(folder, permits, lambda:budget, trigger)
    patrol._config = NS(_optional_actions=coordinator, optional_action=coordinator.dispatch)
    patrol._patrol_cfg=lambda:NS(enabled=True,proactive_trigger_enabled=True,miss_enabled=True)
    patrol.initialize_disposition=AsyncMock(return_value={"anchor_hours":6,"loneliness":1.0})
    patrol._last_target_message_text=lambda:None
    patrol._sleep_hours_between=lambda *a:0
    patrol._miss_state_inputs=lambda:(.95,.95,.5,3,0)
    patrol._mood_effect=lambda:{}
    patrol._maybe_publish_silence=AsyncMock()
    patrol._miss_suppressed_by_recent_speak=lambda *a:False
    patrol._experience_share_motive=lambda:None
    patrol._format_miss_reason=lambda *a,**kw:"synthetic"
    patrol._append_intent_hint=lambda s:s
    patrol._publish_screen_narration=AsyncMock()
    patrol._discard_screen_narration=lambda:None
    patrol._log_decision=lambda **kw:None
    with patch("companion.patrol.datetime",Clock),patch("companion.patrol.random.random",return_value=0),\
         patch("companion.patrol.register_turn",return_value="turn"),patch("companion.patrol.discard_turn"),\
         patch("integrations.optional_action.time.time",side_effect=lambda:epoch[0]):
        await patrol._tick()
        assert patrol._last_trigger_status == "enqueued"
        await coordinator.confirm_reply("private-test")
        assert patrol.mark_miss_spoken()
        epoch[0]=anchor+30*3600
        await patrol._tick()
    status2=patrol._last_trigger_status
    claims=dict(mem.doc["cycles"]["10001"]["claims"])
    assert status2 == "enqueued" and trigger.await_count == 2, (status2,claims)
    return


def write_doc(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


async def bound_legacy_upgrade(folder):
    old = {"version":3, "score":111.25, "baseline":83.0, "tier":2,
           "habits":{"synthetic":"old-A"}, "impressions":[{"text":"fixture", "date":datetime.now().date().isoformat()}]}
    path = folder/"affection_memory.json"
    write_doc(path, old)
    before = path.read_bytes()
    write_doc(folder/"screen_target_scope.json", {"target_qq":"10001", "stream_id":"A"})
    write_doc(folder/"initiative_state.json", {"version":1,"cycles":{"10001":{"claims":{}}}})
    manager = AffectionManager(folder, target_bound=True)
    assert manager.impressions() == [] and not manager.is_enabled()
    assert manager.bind_target("10001")
    assert (manager.score(), manager.baseline(), manager.tier(), manager.get_habit("synthetic")) == (111.25,83,2,"old-A")
    assert len(manager.impressions()) == 1
    generation = manager.binding_generation
    assert not manager.bind_target("10001") and manager.binding_generation == generation
    manager.bind_target("10002")
    assert manager.get_habit("synthetic") is None and not manager.impressions()
    manager.set_habit("synthetic", "new-B")
    manager.flush()
    restarted = AffectionManager(folder, target_bound=True)
    restarted.bind_target("10001")
    assert restarted.score() == 111.25 and restarted.get_habit("synthetic") == "old-A"
    restarted.bind_target("10002")
    assert restarted.get_habit("synthetic") == "new-B"
    assert path.read_bytes() == before


async def contested_legacy_preserved(folder):
    path = folder/"affection_memory.json"
    write_doc(path, {"version":3,"score":150,"habits":{"private":"former"}})
    before = path.read_bytes()
    write_doc(folder/"screen_target_scope.json", {"target_qq":"10001","stream_id":"A"})
    write_doc(folder/"initiative_state.json", {"version":1,"cycles":{"10002":{"claims":{}}}})
    manager = AffectionManager(folder, target_bound=True)
    manager.bind_target("10001")
    assert manager.get_habit("private") is None
    write_doc(folder/"initiative_state.json", {"version":1,"cycles":{"10001":{"claims":{}}}})
    restarted = AffectionManager(folder, target_bound=True)
    restarted.bind_target("10001")
    restarted.bind_target("10002")
    assert restarted.get_habit("private") is None and path.read_bytes() == before


async def late_review_ownership(folder):
    manager = AffectionManager(folder, target_bound=True)
    manager.bind_target("10001")
    manager.sync_daily()
    reviewer = AffectionReviewer(NS(config=NS()),manager,llm=NS())
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"delta_score":3, "impressions":[{"text":"only-A"}], "tone":"only-A", "reason":"fixture"}
    with patch("companion.affection_referee.run_daily_review", delayed):
        old = asyncio.create_task(reviewer.perform_daily_review())
        await entered.wait()
        manager.bind_target("10002")
        release.set()
        result = await old
    assert not result["ok"] and manager.score() == 60 and not manager.impressions()


async def fallback_recovery_once(folder):
    affinity = AffectionManager(folder)
    affinity.sync_daily()
    affinity.append_event_entry({"kind":"chat_round","desc":"record"})
    day = datetime.now().date().isoformat()
    failed = AffectionReviewer(NS(config=NS()),affinity,llm=NS(generate=AsyncMock(return_value="")))
    assert not (await failed.perform_daily_review(date=day))["ok"]
    recovered = AffectionManager(folder)
    assert recovered.event_log()[0]["desc"] == "record"
    real = NS(generate=AsyncMock(return_value='{"delta_score":1,"impressions":[],"tone":"平静","reason":"已评估"}'))
    reviewer = AffectionReviewer(NS(config=NS()),recovered,llm=real)
    results = await asyncio.gather(reviewer.perform_daily_review(date=day), reviewer.perform_daily_review(date=day))
    assert sum(x["ok"] for x in results) == 1 and real.generate.await_count == 1
    after_restart = AffectionManager(folder)
    assert after_restart.score() == 61 and after_restart.review_state()["daily_reviewed_date"] == day


async def previous_successful_cache(folder):
    manager = WatchTaskManager(folder)
    manager._kick = lambda: None
    manager._store.save_cache(A, {"video":{"bvid":A,"title":"before"},"content_description":"known success"})
    cache = manager._store.cache_path(A)
    before = cache.read_bytes(), cache.stat().st_mtime_ns
    claimed = manager.submit(A, owner="tool", rewatch=True)
    identity = claimed["task_id"]
    task = manager._store.get_task(identity)
    task.update(status="running",started_at=time.time())
    manager._store.upsert_task(task)
    entered = asyncio.Event()
    async def late(result):
        entered.set()
        await asyncio.Future()
    manager.set_impression_hook(late)
    finishing = asyncio.create_task(manager._finish(task, {"status":"completed","video":{"bvid":A,"title":"temporary"},
                                                           "finished_at":time.time(),"content_description":"partial"}))
    await entered.wait()
    manager.cancel(identity, force=True)
    await finishing
    assert manager.submit(A,owner="tool")["cached"]
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before


async def detached_and_cross_midnight(folder):
    day = ["day-one"]
    quota = SurfQuotaLedger(folder, day_provider=lambda:day[0],clock=lambda:NOW)
    async def api(action, **kw):
        return ({"success":True,"status":"completed","started_at":NOW-60} if action=="status" else
                {"success":True,"result":{"status":"completed","finished_at":NOW,"started_at":NOW-60}})
    coord = VideoWatchCoordinator(folder, api,lambda:None,lambda _:None,quota=quota,clock=lambda:NOW)
    assert quota.reserve_once("shared", "dedicated", 10)
    entry={"scenario":"dedicated","task_id":"t","detached":True,"closed":True,"reserved_minutes":10,"quota_day":day[0]}
    assert await coord._poll_detached("shared",entry)
    assert quota.snapshot()["minutes_used"] == 1 and not coord._can_continue_watching(entry)
    assert coord._settle_quota("shared",entry) is False
    day[0]="day-two"
    assert quota.reserve_once("shared", "dedicated", 10)
    old={"reserved_minutes":10,"watched_minutes_accumulated":1,"quota_day":"day-one"}
    assert not coord._settle_quota("shared",old)
    assert quota.snapshot()["minutes_used"] == 10


async def quota_reconciliation(folder):
    quota=SurfQuotaLedger(folder,day_provider=lambda:DAY,clock=lambda:NOW)
    assert quota.reserve_once("gap-old","gap",10)
    c=VideoWatchCoordinator(folder,AsyncMock(side_effect=AssertionError("no watch rerun")),
                            lambda:None,lambda _:None,quota=quota,clock=lambda:NOW,config_provider=lambda:NS(enabled=False))
    entry={"scenario":"gap","closed":True,"result":{"title":"done"},"stage":"completed",
           "watched_minutes_accumulated":2,"reserved_minutes":10,"quota_day":DAY,"started_at":NOW-120}
    c._save_links({"links":{"gap-old":entry}})
    with patch("integrations.video_schedule.time.time",return_value=NOW):
        await c.sync()
        c._save_links({"links":{"gap-old":entry}})  # replay after the quota write and before link-file replacement
        await c.sync()
    assert quota.snapshot()["minutes_used"] == 2 and c.episode_state("gap-old")["settled_minutes"] == 2


async def retired_watch_guards(folder):
    from integrations.qzone_video_bridge import QzoneVideoLedger
    ledger=QzoneVideoLedger(folder/"receipts.sqlite")
    with patch("integrations.qzone_video_bridge.CANDIDATE_CAPACITY",2):
        for bvid, tid in [(A,"safe"),(B,"unknown")]:
            assert ledger.observe({"bvid":bvid,"title":"sample"},tid,"episode",NOW,10)
        unknown=next(r for r in ledger.snapshot()["candidates"] if r["task_id"]=="unknown")
        claim=ledger.claim_publish(unknown["candidate_id"],B,"unknown",NOW)
        ledger.mark_attempt_sending(claim["attempt_id"],"req",NOW)
        ledger.finish_attempt(claim["attempt_id"],"uncertain",NOW)
        ledger.record_uncertain(unknown["candidate_id"],NOW,"unknown")
        ledger.expire(NOW+30)
        assert ledger.observe({"bvid":"BV0000000003","title":"new"},"new","episode",NOW+60,10)
        assert ledger.get_candidate(unknown["candidate_id"])["status"] == "uncertain"
        assert ledger.attempt_row(claim["attempt_id"])["status"] == "uncertain"
        assert not ledger.observe({"bvid":A,"title":"repeat"},"safe","episode",NOW+60,10)


async def write_fallback_only_when_unsent(folder):
    callback = AsyncMock(side_effect=[{"success":False,"error":"API not registered"},
                                     {"status":"ok","data":{"comment_id":"7"}}])
    q = QzoneService(NS(api=NS(call=callback)),NS(qzone=NS()),NS(),NS())
    assert await q.comment_moment("post","text") == "7"
    assert callback.await_count == 2


async def audience_scope_and_quality_schema(folder):
    from video_watch.share_config import VideoShareSectionConfig
    cfg = VideoShareSectionConfig(allow_all_groups=False, allowed_chats=["private:10002","group:30001"])
    ledger=ShareLedger(folder/"shares.sqlite")
    registered=[ROUTE,{"stream_id":"extra","user_id":"10002"},{"stream_id":"group","group_id":"30001"}]
    profiles=AudienceProfiles(ledger,stream_reader=AsyncMock(return_value=registered),
                              message_reader=AsyncMock(),config_provider=lambda:cfg,target_provider=lambda:ROUTE)
    assert {row["stream_id"] for row in await profiles.routes()} == {ROUTE["stream_id"],"group"}
    assert "空间" in type(cfg).model_fields["video_share_quality_gate"].description
    assert "其他 private:QQ号不加入候选" in type(cfg).model_fields["allowed_chats"].description


async def screen_binding_authority(folder):
    from companion.screen_tools import ScreenToolsMixin, remember_screen_target, screen_registration_stream
    p=NS(ctx=NS(paths=NS(data_dir=folder)),config=NS(patrol=NS(target_qq="",_host_qq_admin="10001")))
    path=folder/"screen_target_scope.json"
    write_doc(path,{"target_qq":"10001","stream_id":"old"})
    assert screen_registration_stream(p) == ""
    assert remember_screen_target(p,"10001","live")
    assert screen_registration_stream(p) == "live"
    p.config.patrol.target_qq="10002"
    assert screen_registration_stream(p) == ""
    assert remember_screen_target(p,"10002","explicit")
    p.config.patrol.target_qq=""
    assert screen_registration_stream(p) == ""
    assert remember_screen_target(p,"","") and not json.loads(path.read_text(encoding="utf-8"))["stream_id"]
    tool=ScreenToolsMixin()
    tool._screen_scope_ready=False
    assert not tool._screen_tool_allowed({"stream_id":"explicit","user_id":"10002"})


async def group_and_zero_limits(folder):
    assert boosted_limits({"daily_limit":0,"stream_daily_limit":1},3)["daily_limit"] == 0
    _, social=fixtures(folder)
    assert social._target_multiplier({"kind":"group","group_id":"30001","stream_id":"g"}) == 1
    assert social._target_multiplier(ROUTE) == 2


async def publication_rechecks(folder):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026,6,4,12)
    cfg=NS(qzone=NS(enabled=True,daily_max_posts=0,post_cooldown_minutes=120,
                    post_llm_gate_enabled=False),schedule=NS(enabled=True))
    svc=QzoneService(NS(),cfg,NS(),NS())
    svc.bind_state_dir(folder)
    svc._kick_post_media_refresh=lambda:None
    svc.call_action=AsyncMock(return_value=(True,{"tid":"only-approved"},""))
    with patch("qzone.service.datetime",Clock), patch("qzone.service._sleep_silence_window",return_value=("23:00","07:00")):
        assert await svc.post_moment("quota blocked",enforce_daily_limit=True) is None
        assert await svc.post_moment("video mode changed",recheck_gates=True,dispatch_allowed=lambda:False) is None
        assert await svc.post_moment("explicit admin",recheck_gates=True)
        cfg.qzone.daily_max_posts=7
        assert await svc.post_moment("new content during cooldown",enforce_daily_limit=True) is None
        assert await svc.post_moment("explicit admin",recheck_gates=True) == {"tid":"only-approved"}
        assert svc.call_action.await_count == 1


async def refused_batch_write_retries(folder):
    affinity=AffectionManager(folder)
    affinity.sync_daily()
    affinity.append_event_entry({"kind":"fixture","desc":"retain"})
    reviewer=AffectionReviewer(NS(config=NS()),affinity,llm=NS(generate=AsyncMock(
        return_value='{"delta_score":2,"tone":"晴","impressions":[],"reason":"合成"}')))
    date=datetime.now().date().isoformat()
    from companion import affection as store
    write=store.save_json_atomic
    def store_when_pending(path,data):
        if data.get("review",{}).get("daily_reviewed_date")==date and "pending_review" not in data:
            return False
        return write(path,data)
    with patch("companion.affection.save_json_atomic",store_when_pending):
        result=await reviewer.perform_daily_review(date=date)
    assert not result["ok"] and affinity.score()==60 and affinity.event_log()[0]["desc"]=="retain"
    assert (await reviewer.perform_daily_review(date=date))["ok"] and affinity.score()==62
    assert AffectionManager(folder).score()==62


async def protected_asset_and_pending_fact(folder):
    from integrations.qzone_video_bridge import QzoneVideoLedger
    ledger=QzoneVideoLedger(folder/"safe.sqlite")
    with patch("integrations.qzone_video_bridge.CANDIDATE_CAPACITY",2):
        assert ledger.observe({"bvid":A},"with-asset","e",NOW,60)
        assert ledger.observe({"bvid":B},"with-fact","e",NOW,60)
        ids={x["task_id"]:x["candidate_id"] for x in ledger.snapshot()["candidates"]}
        asset="vframe_"+"a"*32
        ledger.asset_dir.mkdir(exist_ok=True)
        ledger._asset_path(asset).write_bytes(b"asset")
        ledger.register_assets([{"asset_id":asset,"bvid":A,"cid":"1","timestamp_seconds":0,
                                 "width":100,"height":100,"bytes":5,"mime":"image/jpeg","sha256":"a"*64}],
                               "with-asset",NOW,3600)
        ledger.record_posted(ids["with-fact"],"published",NOW,fact_sync="pending")
        ledger.expire(NOW+100)
        assert ledger.get_assets([asset])
        assert not ledger.observe({"bvid":"BV0000000003"},"next","e",NOW+100,60)
        ledger.record_fact_sync_result(ids["with-fact"],"ok",1)
        assert ledger.observe({"bvid":"BV0000000003"},"next","e",NOW+101,60)
        assert ledger.get_candidate(ids["with-asset"])


async def qzone_policy_equivalence(folder):
    from qzone.decision import post_rule_rejection
    from itertools import product
    for asleep, quiet, missing, nap, cooling in product((False, True), repeat=5):
        gate, reason = post_rule_rejection(
            asleep=asleep, quiet=quiet, require_activity=missing, has_activity=not missing,
            actual_nap=nap, cooldown_minutes=7 if cooling else 0)
        expected = ("silence" if asleep or quiet else "no_activity" if missing else
                    "nap_quiet" if nap else "cooldown" if cooling else "")
        assert gate == expected
        assert bool(reason) == bool(gate)

    config = NS(qzone=NS(enabled=True, daily_max_posts=2, post_cooldown_minutes=120,
                        post_llm_gate_enabled=False, require_activity=False), schedule=NS(enabled=True))
    service = QzoneService(NS(), config, NS(find_current_activity=lambda *args, **kw: None), NS())
    now = datetime(2026, 6, 4, 12)
    service._state["last_post_at"] = "2026-06-04T11:00:00"
    with patch("qzone.service._sleep_silence_window", return_value=("23:00", "07:00")):
        assert not (await service.check_post_gates(now))[0]
        assert not await service._post_gate_chain("2026-06-04", now)
        assert not service._dispatch_gate(now, enforce_daily_limit=True,
                                          recheck_gates=False, dispatch_allowed=None)
        assert service._cooldown_remain_minutes(now, retry=True) == 60
    service._state["last_post_at"] = "unknown"
    assert service._cooldown_remain_minutes(now) == 0
    assert service._cooldown_remain_minutes(now, retry=True) > 0


async def watch_terminal_equivalence(folder):
    from itertools import product
    for scenario, stage, detached in product(("dedicated", "gap", "casual"),
                                             ("completed", "failed", "cancelled"), (False, True)):
        case_dir = folder / f"{scenario}-{stage}-{detached}"
        case_dir.mkdir()
        quota = SurfQuotaLedger(case_dir, day_provider=lambda: DAY, clock=lambda: NOW)
        assert quota.reserve_once("episode", scenario, 10)
        status = {"success": True, "status": stage, "started_at": NOW-120, "finished_at": NOW}
        async def api(action, **kwargs):
            if action == "status":
                return dict(status)
            if action == "result":
                return {"success": True, "result": {**status, "video": {"bvid": A, "title": "fixture"}}}
            raise AssertionError(action)
        released = []
        attention = NS(lease_valid=lambda _: True, release=lambda owner: released.append(owner))
        coordinator = VideoWatchCoordinator(case_dir, api, lambda: {}, lambda _: {},
                                             quota=quota, clock=lambda: NOW, attention=attention)
        entry = {"scenario": scenario, "task_id": "t", "stage": "running", "quota_day": DAY,
                 "reserved_minutes": 10, "session_target_minutes": 10, "lease": {"kind": "surf"}}
        if detached:
            entry.update(detached=True, closed=True)
            assert await coordinator._poll_detached("episode", entry)
        else:
            assert await coordinator._poll_status("episode", entry)
        assert entry["stage"] == stage and entry["closed"] and entry["closed_reason"] == "task_" + stage
        assert not entry.get("lease")
        assert released == ["schedule:episode"]
        if stage == "completed" and scenario == "dedicated" and not detached:
            assert quota.snapshot()["minutes_used"] == 10 and entry["reserved_minutes"] == 10
        else:
            assert quota.snapshot()["minutes_used"] == 2 and entry["settled_minutes"] == 2
        if stage == "completed":
            assert entry["watched_minutes_accumulated"] == 2
        else:
            assert not entry.get("result")
        if detached:
            assert entry["detached_finished"] and not entry["detached"]


async def optional_weekly_summary_failure(folder):
    manager = AffectionManager(folder)
    manager.sync_daily()
    manager.append_event_entry({"kind": "chat_round", "desc": "submitted"})
    manager.bump_cycle_rounds()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def verdict(*args, **kwargs):
        entered.set()
        await finish.wait()
        return {"delta_score": 2, "tone": "平静", "impressions": [{"text": "synthetic"}], "reason": "合成"}

    reviewer = AffectionReviewer(NS(config=NS()), manager, llm=NS())
    day = datetime.now().date().isoformat()
    with patch("companion.affection_referee.run_daily_review", verdict), patch.object(
        manager, "record_daily_summary", side_effect=RuntimeError("optional weekly summary failed")
    ):
        task = asyncio.create_task(reviewer.perform_daily_review(date=day))
        await entered.wait()
        manager.append_event_entry({"kind": "chat_round", "desc": "arrived later"})
        manager.bump_cycle_rounds()
        finish.set()
        result = await task
    assert result["ok"] and manager.score() == 62
    assert [row["desc"] for row in manager.event_log()] == ["arrived later"]
    assert manager.cycle_rounds() == 1 and manager.impressions()[-1]["text"] == "synthetic"
    restored = AffectionManager(folder)
    assert restored.score() == 62 and restored.review_state()["daily_reviewed_date"] == day
    again = AffectionReviewer(NS(config=NS()), restored, llm=NS())
    assert (await again.perform_daily_review(date=day))["reason"] == "already_reviewed"
    assert restored.score() == 62 and [row["desc"] for row in restored.event_log()] == ["arrived later"]


CASES = [
    "optional_weekly_summary_failure",
    "watch_terminal_equivalence",
    "qzone_policy_equivalence",
    "share_limits", "wrong_manual_receipt", "profile_count", "interrupted_eval", "cancel_cache",
    "gap_quota", "identity_binding", "review_concurrency", "qzone_capacity",
    "qzone_write_timeout", "qzone_limit_race", "qzone_activity_toggle", "watch_debit_atomicity",
    "fallback_review", "candidate_overlap", "tiered_miss", "bound_legacy_upgrade",
    "contested_legacy_preserved", "late_review_ownership", "fallback_recovery_once",
    "previous_successful_cache", "detached_and_cross_midnight", "quota_reconciliation",
    "retired_watch_guards", "write_fallback_only_when_unsent",
    "audience_scope_and_quality_schema", "screen_binding_authority", "group_and_zero_limits",
    "publication_rechecks", "refused_batch_write_retries", "protected_asset_and_pending_fact",
]


async def run_case(name):
    bootstrap()
    with tempfile.TemporaryDirectory(prefix='maibot-test-') as folder:
        await globals()[name](Path(folder))


class CompanionMediaTests(unittest.TestCase):
    """The host SDK imports and socket guard never leak into other test modules."""


def isolated_case(name):
    def run(self):
        result = subprocess.run([sys.executable, '-B', __file__, '--case', name],
                                capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
    return run


for case_name in CASES:
    setattr(CompanionMediaTests, 'test_' + case_name, isolated_case(case_name))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--case':
        logging.basicConfig(level=logging.ERROR)
        sys.stderr.reconfigure(encoding='utf-8')
        asyncio.run(run_case(sys.argv[2]))
    else:
        unittest.main()
