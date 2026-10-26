"""纯逻辑与宿主边界测试；网络、数据库和通知均由 fixture 替代。"""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.plugins.mteaminvitereminder import DEFAULTS, MTeamInviteReminder
from app.plugins.mteaminvitereminder import core
import app.plugins.mteaminvitereminder as module

NOW = datetime(2026, 10, 10, 12, tzinfo=core.TZ)


def profile(days=31, role=1, parked=False, uid=2):
    return {"id": uid, "parentId": 1, "username": "test", "enabled": True,
            "role": role, "parked": parked,
            "memberStatus": {"lastBrowse": (NOW - timedelta(days=days)).isoformat()}}


@pytest.fixture
def plugin(monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(module, "datetime", FixedClock)
    instance = object.__new__(MTeamInviteReminder)
    instance._lock = Lock()
    instance._stop = Event()
    instance._scheduler = None
    instance._config = dict(DEFAULTS, enabled=True)
    monkeypatch.setattr(module, "SiteOper", Mock(return_value=SimpleNamespace(list_order_by_pri=lambda: [])))
    instance._config_error = None
    data = {}
    instance.get_data = lambda key: deepcopy(data.get(key))
    instance.save_data = lambda key, value: data.__setitem__(key, deepcopy(value))
    instance.update_config = Mock()
    instance.post_message = Mock()
    return instance


@pytest.mark.parametrize("days,expected", [(29, False), (30, False), (30.0001, True), (31, True), (39, True), (40, False), (41, False)])
def test_exact_threshold(days, expected):
    result = core.assess(profile(days), {"id": 2}, 1, NOW)
    assert result["eligible"] is expected


@pytest.mark.parametrize("role", [7, 8, 10, 16])
def test_high_and_special_roles_skipped(role):
    assert not core.assess(profile(role=role), {"id": 2}, 1, NOW)["eligible"]


def test_parked_rules():
    assert not core.assess(profile(role=6, parked=True), {"id": 2}, 1, NOW)["eligible"]
    result = core.assess(profile(70, role=5, parked=True), {"id": 2}, 1, NOW)
    assert result["eligible"] and result["limit"] == 90
    assert not core.assess(profile(90, parked=True), {"id": 2}, 1, NOW)["eligible"]


@pytest.mark.parametrize("key,value", [("id", 3), ("parentId", 4), ("parentId", None), ("parked", None), ("role", None), ("memberStatus", None)])
def test_missing_or_wrong_fields_fail_closed(key, value):
    data = profile()
    data[key] = value
    with pytest.raises(core.DataError):
        core.assess(data, {"id": 2}, 1, NOW)


def test_disabled_and_false_string():
    data = profile()
    data["enabled"] = "false"
    assert not core.assess(data, {"id": 2}, 1, NOW)["eligible"]
    assert core.boolean("false") is False


def test_last_login_cannot_replace_browse():
    data = profile()
    data["memberStatus"] = {"lastLogin": "2026-09-01 12:00:00"}
    with pytest.raises(core.DataError):
        core.assess(data, {"id": 2}, 1, NOW)


def test_timezone_and_future():
    assert core.parse_time("2026-10-10T04:00:00Z") == NOW
    assert core.parse_time("2026-10-10 12:00:00") == NOW
    with pytest.raises(core.DataError):
        core.assess(profile(-1), {"id": 2}, 1, NOW)


def test_roster_rejects_unknown_and_pagination():
    assert core.api_roster([{"id": "2"}, {"id": 2}]) == [{"id": 2}]
    assert core.api_roster({"data": [{"id": 2}], "total": 1}) == [{"id": 2}]
    assert core.manual_roster("2,3\n2") == [{"id": 2}, {"id": 3}]
    for data in ({"quota": 20}, {"data": [{"id": 2}], "total": 3}, [{"id": 2, "uid": 3}], [{"id": True}], {"data": [], "totalPages": 2}):
        with pytest.raises(core.DataError):
            core.api_roster(data)


def test_live_history_uid_shape():
    rows = core.api_roster([{"uid": "2", "username": "test", "status": "NORMAL"}])
    assert rows[0]["id"] == 2


class NoWait:
    """测试不实际等待；不替换生产的限速实现。"""
    def wait(self, _seconds):
        return False

    def is_set(self):
        return False

    def set(self):
        pass


def configure_scan(plugin, profiles, roster_ids=None):
    plugin._stop = NoWait()
    plugin._site = Mock(return_value=object())
    roster = [{"id": uid} for uid in (profiles if roster_ids is None else roster_ids)]
    def request(_site, path, uid=None):
        if path == "/member/profile" and uid is None:
            return {"id": 1}
        if path.startswith("/invite/"):
            return roster
        return profiles[uid]
    plugin._request = Mock(side_effect=request)


def test_preview_reads_all_without_notification(plugin):
    configure_scan(plugin, {2: profile()})
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert result["rows"][0]["eligible"]
    assert result["notification"].startswith("预览模式")
    plugin.post_message.assert_not_called()
    assert "email" not in repr(result) and "apikey" not in repr(result)


def test_notify_eligible_account_on_every_check(plugin):
    configure_scan(plugin, {2: profile()})
    plugin._config["notify"] = True
    plugin.run_check()
    plugin.run_check()
    assert plugin.post_message.call_count == 2
    assert "UID 2" in plugin.post_message.call_args.kwargs["text"]
    assert "符合提醒 1 人" in plugin.post_message.call_args.kwargs["text"]
    assert plugin.get_data("last_result")["rows"][0]["notification"] == "已提交 MP 通知渠道"


def test_legacy_notification_record_does_not_suppress_reminder(plugin):
    configure_scan(plugin, {2: profile()})
    plugin._config["notify"] = True
    last_browse = core.assess(profile(), {"id": 2}, 1, NOW)["last_browse"]
    legacy_key = sha256(f"1|2|{last_browse}".encode()).hexdigest()
    plugin.save_data("notified", {legacy_key: NOW.isoformat()})
    plugin.run_check()
    plugin.post_message.assert_called_once()


def test_no_reminder_when_accounts_no_longer_eligible(plugin):
    configure_scan(plugin, {2: profile()})
    plugin._config["notify"] = True
    plugin.run_check()
    configure_scan(plugin, {2: profile(days=1)})
    plugin.run_check()
    plugin.post_message.assert_called_once()
    assert plugin.get_data("last_result")["notification"] == "本次无待提醒账号"


@pytest.mark.parametrize("value,expected", [(None, []), ("", []), (", ,", []),
                                            ("2, 3,2", [2, 3]), ("2，3\n4", [2, 3, 4])])
def test_optional_manual_uid_list(value, expected):
    assert [row["id"] for row in core.manual_roster(value)] == expected


@pytest.mark.parametrize("parent", [None, 99])
def test_explicit_manual_accounts_can_belong_to_another_inviter(parent):
    data = dict(profile(), parentId=parent)
    assert core.assess(data, {"id": 2}, 1, NOW, manual=True)["eligible"]
    with pytest.raises(core.DataError):
        core.assess(data, {"id": 2}, 1, NOW)


def test_manual_account_still_requires_matching_uid_and_excludes_owner():
    for data, row in ((profile(uid=3), {"id": 2}), (profile(uid=1), {"id": 1})):
        with pytest.raises(core.DataError):
            core.assess(data, row, 1, NOW, manual=True)


@pytest.mark.parametrize("value", ["2,abc", "0", 0, "2,-3", "2.5"])
def test_invalid_manual_uids_disable_check_without_network(plugin, value):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, manual_uids=value))
    plugin._request = Mock()
    plugin.run_check()
    assert not plugin.get_state() and plugin.get_service() == []
    assert "UID" in plugin._config_error
    plugin._request.assert_not_called()


def test_manual_accounts_append_after_roster_and_deduplicate_in_order(plugin):
    configure_scan(plugin, {2: profile(), 3: profile(uid=3),
                            4: dict(profile(uid=4), parentId=None),
                            5: dict(profile(uid=5), parentId=99)}, roster_ids=[2, 3])
    plugin._config.update(notify=True, manual_uids="3,5,4,5")
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert result["total"] == 4
    assert [row["uid"] for row in result["rows"]] == [2, 3, 5, 4]
    assert [row["source"] for row in result["rows"]] == ["直接下级", "直接下级", "手动添加", "手动添加"]
    assert all(row["eligible"] for row in result["rows"])
    requests = [call.args[2] for call in plugin._request.call_args_list
                if call.args[1] == "/member/profile" and len(call.args) == 3]
    assert requests == [2, 3, 5, 4]
    assert "符合提醒 4 人" in plugin.post_message.call_args.kwargs["text"]
    assert "手动添加" in repr(plugin.get_page())


def test_manual_duplicate_cannot_bypass_automatic_parent_validation(plugin):
    configure_scan(plugin, {2: dict(profile(), parentId=99)})
    plugin._config["manual_uids"] = "2,2"
    plugin.run_check()
    rows = plugin.get_data("last_result")["rows"]
    assert len(rows) == 1 and rows[0]["source"] == "直接下级"
    assert not rows[0]["eligible"] and "无法判断" in rows[0]["reason"]


def test_manual_accounts_work_with_empty_automatic_roster(plugin):
    configure_scan(plugin, {2: dict(profile(), parentId=99)}, roster_ids=[])
    plugin._config.update(notify=True, manual_uids="2")
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert result["status"] == "completed" and result["total"] == 1
    assert result["rows"][0]["source"] == "手动添加" and result["rows"][0]["eligible"]
    plugin.post_message.assert_called_once()


def test_bad_manual_account_does_not_stop_later_accounts(plugin):
    configure_scan(plugin, {2: profile(), 3: {"id": 3},
                            4: dict(profile(uid=4), parentId=99)}, roster_ids=[2])
    plugin._config.update(notify=True, manual_uids="3,4")
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert result["status"] == "completed" and len(result["rows"]) == 3
    assert result["rows"][1]["reason"].startswith("无法判断")
    assert result["rows"][2]["eligible"]
    assert "无法判断 1 人" in plugin.post_message.call_args.kwargs["text"]


def test_legacy_site_message_config_is_ignored(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, notify=True, site_notify=True))
    configure_scan(plugin, {2: profile()})
    plugin.run_check()
    form, defaults = plugin.get_form()
    assert "site_notify" not in plugin._config and "site_notify" not in defaults
    assert "站内信" not in repr(form)
    assert {call.args[1] for call in plugin._request.call_args_list} == {"/member/profile", module.INVITE_PATH}
    plugin.post_message.assert_called_once()


def test_legacy_site_message_results_are_not_displayed(plugin):
    marker = "旧版站内信发送失败"
    plugin.save_data("last_result", {"status": "completed", "site_notification": marker,
                                   "rows": [{"uid": 2, "site_notification": marker}]})
    assert "站内信" not in repr(plugin.get_page())


def test_large_notifications_not_truncated_or_silently_marked(plugin):
    configure_scan(plugin, {uid: profile(uid=uid) for uid in range(2, 34)})
    plugin._config["notify"] = True
    plugin.run_check()
    assert plugin.post_message.call_count == 3
    rows = plugin.get_data("last_result")["rows"]
    assert all(row["notification"] == "已提交 MP 通知渠道" for row in rows)
    text = "\n".join(call.kwargs["text"] for call in plugin.post_message.call_args_list)
    assert all(f"UID {uid}）" in text for uid in range(2, 34))


def test_partial_notification_failure_does_not_mark_unsent_batch(plugin):
    configure_scan(plugin, {uid: profile(uid=uid) for uid in range(2, 22)})
    plugin._config["notify"] = True
    plugin.post_message.side_effect = [None, RuntimeError("private-token"), None]
    plugin.run_check()
    rows = plugin.get_data("last_result")["rows"]
    assert all(row["notification"] == "已提交 MP 通知渠道" for row in rows[:15])
    assert all(row["notification"] == "待通知" for row in rows[15:])
    assert "private-token" not in repr(plugin.get_data("last_result"))
    assert plugin.get_data("last_result")["error"]


def test_bad_child_is_unknown_not_normal(plugin):
    configure_scan(plugin, {2: {"id": 2}, 3: profile(uid=3)})
    plugin._config["notify"] = True
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert result["rows"][0]["reason"].startswith("无法判断")
    assert result["rows"][1]["eligible"]
    assert plugin.post_message.call_count == 1


def test_api_request_tls_header_uid_and_no_redirect(plugin, monkeypatch):
    transport = Mock()
    transport.post_res.return_value = SimpleNamespace(status_code=200, json=lambda: {"code": "0", "data": {"id": 2}})
    factory = Mock(return_value=transport)
    monkeypatch.setattr(module, "RequestUtils", factory)
    site = SimpleNamespace(apikey="secret", proxy=False, ua="test")
    assert plugin._request(site, "/member/profile", 2) == {"id": 2}
    assert factory.call_args.kwargs["verify"] is True
    assert factory.call_args.kwargs["headers"]["x-api-key"] == "secret"
    assert transport.post_res.call_args.kwargs["params"] == {"uid": 2}
    assert transport.post_res.call_args.kwargs["allow_redirects"] is False
    transport.post_res.return_value.status_code = 302
    with pytest.raises(core.DataError):
        plugin._request(site, "/member/profile")


def test_internal_endpoint_settings_are_ignored(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, api_base="https://attacker.invalid/api", invite_path="/bad"))
    assert plugin._api_base() == "https://api.m-team.cc/api"
    assert "api_base" not in plugin._config


def test_lifecycle_invalid_cron_no_task_and_reset_one_shot(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, cron="bad", onlyonce=True))
    assert not plugin.get_state()
    assert plugin.get_service() == []
    assert plugin._scheduler is None
    assert plugin.update_config.call_args.args[0]["onlyonce"] is False
    plugin.stop_service()
    plugin.stop_service()


def test_lock_prevents_overlapping_checks(plugin):
    plugin._scan = Mock()
    plugin._lock.acquire()
    try:
        plugin.run_check()
    finally:
        plugin._lock.release()
    plugin._scan.assert_not_called()


def test_details_use_text_not_html(plugin):
    plugin.save_data("last_result", {"rows": [{"uid": 2, "username": "<script>x</script>", "reason": "待提醒"}]})
    page = plugin.get_page()
    assert "<script>x</script>" in repr(page)
    assert "'html'" not in repr(page)
    assert "VTable" in repr(page)


def test_form_uses_grid_and_hides_internal_fields(plugin):
    form, defaults = plugin.get_form()
    assert "VRow" in repr(form) and "VCol" in repr(form)
    assert "manual_uids" in repr(form) and defaults["manual_uids"] == ""
    for key in ("api_base", "invite_path", "roster_source", "request_interval", "exclude_uids"):
        assert key not in repr(form) and key not in defaults


def test_resaving_enabled_config_keeps_inflight_scan_and_snapshot(plugin):
    configure_scan(plugin, {2: profile(), 3: profile(uid=3)})
    old_event = plugin._stop
    request = plugin._request.side_effect
    def saving_request(site, path, uid=None):
        if uid == 2:
            plugin.init_plugin(dict(DEFAULTS, enabled=True, notify=True, manual_uids="4", threshold=35))
            assert plugin._stop is old_event
        return request(site, path, uid)
    plugin._request.side_effect = saving_request
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert result["status"] == "completed" and len(result["rows"]) == 2
    assert all(row["eligible"] for row in result["rows"])
    plugin.post_message.assert_not_called()


def test_disabling_during_scan_preserves_partial_results(plugin):
    configure_scan(plugin, {2: profile(), 3: profile(uid=3)})
    class FastEvent(Event):
        def wait(self, timeout=None):
            return self.is_set()
    plugin._stop = FastEvent()
    request = plugin._request.side_effect
    def disabling_request(site, path, uid=None):
        if uid == 2:
            plugin.init_plugin(dict(DEFAULTS, enabled=False))
            plugin.init_plugin(dict(DEFAULTS, enabled=True))
        return request(site, path, uid)
    plugin._request.side_effect = disabling_request
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert result["status"] == "stopped" and len(result["rows"]) == 1
    assert result["total"] == 2 and result["error"]


def test_unexpected_child_error_is_logged_and_scan_continues(plugin):
    configure_scan(plugin, {2: profile(), 3: profile(uid=3)})
    request = plugin._request.side_effect
    def failed_request(site, path, uid=None):
        if uid == 2:
            raise RuntimeError("private-token")
        return request(site, path, uid)
    plugin._request.side_effect = failed_request
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert len(result["rows"]) == 2 and result["status"] == "completed"
    assert "RuntimeError" in result["rows"][0]["reason"]
    assert "private-token" not in repr(result)


def test_progress_persisted_before_each_child_request(plugin):
    configure_scan(plugin, {2: profile()})
    request = plugin._request.side_effect
    def observe_request(site, path, uid=None):
        if uid == 2:
            current = plugin.get_data("last_result")
            assert current["status"] == "running"
            assert "正在检查 1/1" in current["stage"]
        return request(site, path, uid)
    plugin._request.side_effect = observe_request
    plugin.run_check()
    assert plugin.get_data("last_result")["status"] == "completed"
