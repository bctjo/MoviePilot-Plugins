"""宿主、HTTP、调度和通知边界均使用替身，不访问真实客户或站点。"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.plugins.uploadprogressmonitor as module
from app.plugins.uploadprogressmonitor import DEFAULTS, UploadProgressMonitor
from app.plugins.uploadprogressmonitor.core import DataError, UNITS

TB = UNITS["TB"]


def task(slot=1, **overrides):
    values = {"visible": True, "enabled": True, "site_id": "10", "uid": "77", "initial": "0", "initial_unit": "TB", "total": "5", "total_unit": "TB", "label": "客户 A"}
    values.update(overrides)
    return {f"task_{slot}_{key}": value for key, value in values.items()}


def site(site_id=10, name="馒头", url="https://kp.m-team.cc"):
    return SimpleNamespace(id=site_id, name=name, url=url, apikey="private-api-key", cookie="private-cookie",
                           ua="Test/MoviePilot", proxy=False)


class NoWait:
    def is_set(self):
        return False

    def wait(self, seconds):
        return False

    def set(self):
        pass


@pytest.fixture
def plugin(monkeypatch):
    instance = object.__new__(UploadProgressMonitor)
    instance._lock, instance._stop = Lock(), Event()
    instance._config, instance._tasks = deepcopy(DEFAULTS), []
    instance._config_error = instance._notice = None
    data = {}
    instance.get_data = lambda key: deepcopy(data.get(key))
    instance.save_data = lambda key, value: data.__setitem__(key, deepcopy(value))
    instance.update_config, instance.post_message = Mock(), Mock()
    monkeypatch.setattr(module, "SiteOper", Mock(return_value=SimpleNamespace(list_order_by_pri=lambda: [site(), site(20, "Audiences", "https://audiences.me/")])))
    monkeypatch.setattr(module.scheduler_sdk, "add_plugin_once_job", Mock(return_value=True))
    monkeypatch.setattr(module.scheduler_sdk, "remove_plugin_once_job", Mock())
    instance.init_plugin(dict(DEFAULTS, enabled=True, **task()))
    instance._stop = NoWait()
    return instance


def sample(uploaded=2 * TB, username="sample"):
    return {"username": username, "uploaded_bytes": uploaded}


def test_default_schedule_is_hourly_and_once_uses_host_scheduler(plugin):
    assert DEFAULTS["cron"] == "0 * * * *" and DEFAULTS["notify"]
    assert str(plugin.get_service()[0]["trigger"]) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='0']"
    plugin.init_plugin(dict(DEFAULTS, enabled=True, onlyonce=True, **task()))
    module.scheduler_sdk.add_plugin_once_job.assert_called_once()
    assert plugin.update_config.call_args.args[0]["onlyonce"] is False


def test_invalid_configuration_and_failed_once_scheduling_do_not_start_network(plugin):
    plugin._read_user = Mock()
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(initial="bad")))
    assert not plugin.get_state() and plugin.get_service() == []
    plugin.run_check()
    plugin._read_user.assert_not_called()
    module.scheduler_sdk.add_plugin_once_job.side_effect = RuntimeError("private-cookie")
    plugin.init_plugin(dict(DEFAULTS, enabled=True, onlyonce=True, **task()))
    assert plugin.get_state() and plugin._notice and "private" not in plugin._notice


def test_all_statuses_are_sent_every_round_and_completed_tasks_keep_refreshing(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(), **task(2, uid="78")))
    plugin._read_user = Mock(side_effect=[sample(2 * TB), sample(5 * TB)] * 2)
    plugin.run_check()
    plugin.run_check()
    assert plugin._read_user.call_count == 4 and plugin.post_message.call_count == 2
    text = plugin.post_message.call_args.kwargs["text"]
    assert "【待完成】" in text and "【已完成】" in text
    assert "当前上传量：2.00 TB" in text and "目标上传量：5.00 TB" in text


def test_same_uid_different_site_uses_each_tasks_site_credentials(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(), **task(2, site_id="20")))
    plugin._read_user = Mock(return_value=sample())
    plugin.run_check()
    calls = plugin._read_user.call_args_list
    assert [(call.args[0].id, call.args[1]) for call in calls] == [(10, 77), (20, 77)]


def test_failed_user_is_separate_from_completed_and_other_users_continue(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(), **task(2, uid="78")))
    plugin._read_user = Mock(side_effect=[RuntimeError("private-cookie"), sample(6 * TB)])
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert [row["status"] for row in result["rows"]] == ["error", "completed"]
    assert "private-cookie" not in repr(result)
    assert "【查询失败】" in plugin.post_message.call_args.kwargs["text"]


def test_failed_refresh_shows_last_good_value_but_never_completed_status(plugin):
    plugin._read_user = Mock(return_value=sample(6 * TB))
    plugin.run_check()
    plugin._read_user.side_effect = DataError("Cookie 失效")
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["status"] == "error" and row["last_uploaded_bytes"] == 6 * TB
    assert "【已完成】" not in plugin.post_message.call_args.kwargs["text"]
    assert "上次成功" in plugin.post_message.call_args.kwargs["text"]


def test_counter_decrease_reverts_completion_and_warns(plugin):
    plugin._read_user = Mock(side_effect=[sample(6 * TB), sample(TB)])
    plugin.run_check()
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["status"] == "pending" and row["delta_bytes"] == -5 * TB
    assert row["warning"] and row["completed_at"] is None


def test_editing_goal_recomputes_completion_without_old_delivery_state(plugin):
    plugin._read_user = Mock(return_value=sample(6 * TB))
    plugin.run_check()
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(total="10")))
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["target_bytes"] == 10 * TB and row["status"] == "pending"
    assert row["completed_at"] is None


def test_task_volume_recomputes_progress_after_reconfigure_and_reaches_notifications(plugin):
    plugin._read_user = Mock(return_value=sample(44 * TB))
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(initial="40", total="10")))
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["initial_bytes"] == 40 * TB and row["percent"] == 40
    assert row["task_uploaded_bytes"] == 4 * TB
    page = repr(plugin.get_page())
    assert "50.00 TB" in page and "40.0%" in page
    assert "本次已完成：4.00 TB" in plugin.post_message.call_args.kwargs["text"]
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(initial="40", total="20")))
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["initial_bytes"] == 40 * TB and row["percent"] == 20
    assert row["target_bytes"] == 60 * TB


def test_changing_initial_and_total_resets_completion_even_with_same_target(plugin):
    plugin._read_user = Mock(return_value=sample(6 * TB))
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(initial="3", total="2")))
    plugin._now = Mock(return_value="2026-10-27 10:00:00")
    plugin.run_check()
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(initial="2", total="3")))
    plugin._now = Mock(return_value="2026-10-27 11:00:00")
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["completed_at"] == "2026-10-27 11:00:00"
    plugin._now = Mock(return_value="2026-10-27 12:00:00")
    plugin.run_check()
    assert plugin.get_data("last_result")["rows"][0]["completed_at"] == "2026-10-27 11:00:00"


def test_notification_submission_status_is_hidden(plugin):
    plugin._read_user = Mock(return_value=sample())
    plugin.run_check()
    assert plugin.post_message.call_count == 1
    assert "已提交" not in repr(plugin.get_page())


@pytest.mark.parametrize("amount,expected_text", [
    ("5.020", "+10.24 GB"), ("5.010", "+0.00 B"), ("5.000", "-10.24 GB"),
])
def test_nexus_displayed_upload_has_delta_after_second_success(plugin, monkeypatch, amount, expected_text):
    def response(value):
        body = (f'<h1><a href="userdetails.php?id=77">sample</a></h1>'
                f'<table><tr><td>上传量</td><td>{value} TB</td></tr></table>')
        return SimpleNamespace(status_code=200, content=body.encode())
    client = Mock()
    client.get_res.side_effect = [response("5.010"), response(amount)]
    monkeypatch.setattr(module, "RequestUtils", Mock(return_value=client))
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(site_id="20", total="10")))
    plugin.run_check()
    assert plugin.get_data("last_result")["rows"][0].get("delta_bytes") is None
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    expected = (int((Decimal(amount) * TB).to_integral_value(rounding=ROUND_CEILING))
                - int((Decimal("5.010") * TB).to_integral_value(rounding=ROUND_CEILING)))
    assert row["delta_bytes"] == expected
    assert bool(row.get("warning")) == (expected < 0)
    assert expected_text in repr(plugin.get_page())
    assert f"较上次：{expected_text}" in plugin.post_message.call_args.kwargs["text"]


def test_nexus_failed_round_keeps_last_success_for_next_delta(plugin):
    before = sample(5 * TB)
    after = sample(6 * TB)
    plugin._read_user = Mock(side_effect=[before, DataError("站点限流"), after])
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(site_id="20", total="10")))
    plugin.run_check()
    plugin.run_check()
    assert plugin.get_data("last_result")["rows"][0]["status"] == "error"
    assert plugin.get_data("last_success")["20:77"]["uploaded_bytes"] == 5 * TB
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["status"] == "pending" and row["delta_bytes"] == TB


def test_deleted_site_does_not_query_another_site(plugin, monkeypatch):
    monkeypatch.setattr(module, "SiteOper", Mock(return_value=SimpleNamespace(list_order_by_pri=lambda: [])))
    plugin._read_user = Mock()
    plugin.run_check()
    plugin._read_user.assert_not_called()
    assert plugin.get_data("last_result")["rows"][0]["status"] == "error"


def test_mteam_request_has_api_key_and_never_cookie_or_redirect(plugin, monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {"code": "0", "data": {"id": 77, "username": "sample", "memberCount": {"uploaded": str(5 * TB)}}})
    client = Mock()
    client.post_res.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "RequestUtils", factory)
    assert plugin._read_user(site(), 77)["uploaded_bytes"] == 5 * TB
    options = factory.call_args.kwargs
    assert options["headers"]["x-api-key"] == "private-api-key" and "cookies" not in options
    assert options["verify"] is True and options["timeout"] == 30
    client.post_res.assert_called_once_with(module.API_BASE + "/member/profile", params={"uid": 77}, allow_redirects=False)


def test_nexus_uses_selected_cookie_and_user_detail_not_site_owner(plugin, monkeypatch):
    body = '<div><strong>上传量</strong>: 99 TB</div><h1><a href="userdetails.php?id=77">sample</a></h1><table><tr><td>上传量</td><td>5.01 TB</td></tr></table>'
    client = Mock()
    client.get_res.return_value = SimpleNamespace(status_code=200, content=body.encode())
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "RequestUtils", factory)
    result = plugin._read_user(site(20, "Audiences", "https://audiences.me/"), 77)
    assert result["uploaded_bytes"] < 6 * TB
    assert factory.call_args.kwargs["cookies"] == "private-cookie"
    assert "x-api-key" not in factory.call_args.kwargs["headers"]
    client.get_res.assert_called_once_with("https://audiences.me/userdetails.php", params={"id": 77}, allow_redirects=False)


def test_audiences_card_response_is_accepted_via_normal_request_path(plugin, monkeypatch):
    body = ('<div class="ud-page"><div class="ud-hero__name"><a href="userdetails.php?id=77">card-user</a></div>'
            '<div class="ud-row"><div class="ud-row__label">UID</div><div class="ud-row__value">77</div></div>'
            '<div class="ud-row"><div class="ud-row__label">上传量</div><div class="ud-row__value">6.125 TB</div></div></div>')
    client = Mock()
    client.get_res.return_value = SimpleNamespace(status_code=200, content=body.encode())
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "RequestUtils", factory)
    sample = plugin._read_user(site(20, "Audiences", "https://audiences.me/"), 77)
    assert sample["username"] == "card-user" and sample["uploaded_bytes"] == 6 * TB + TB // 8
    assert factory.call_args.kwargs["cookies"] == "private-cookie"


def test_nexus_visible_goal_completes_in_saved_result_and_notification(plugin, monkeypatch):
    client = Mock()
    client.get_res.return_value = nexus_response("5.300")
    monkeypatch.setattr(module, "RequestUtils", Mock(return_value=client))
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(site_id="20", initial="0.3", total="5")))
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["status"] == "completed" and row["percent"] == 100
    assert row["remaining_bytes"] == 0 and row["completed_at"]
    assert plugin.get_data("last_success")["20:77"]["uploaded_bytes"] == row["target_bytes"]
    message = plugin.post_message.call_args.kwargs["text"]
    assert "【已完成】" in message and "【待完成】" not in message
    assert "100.0%" in repr(plugin.get_page())


def test_nexus_old_chinese_encoding_and_missing_cookie_are_handled(plugin, monkeypatch):
    body = '<meta charset="gb18030"><h1>示例</h1><table><tr><td>上传量</td><td>5.01 TB</td></tr></table>'
    assert "上传量" in plugin._html(SimpleNamespace(content=body.encode("gb18030")))
    selected = site(20, "Audiences", "https://audiences.me")
    selected.cookie = ""
    factory = Mock()
    monkeypatch.setattr(module, "RequestUtils", factory)
    with pytest.raises(DataError, match="Cookie"):
        plugin._read_user(selected, 77)
    factory.assert_not_called()


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_http_failures_never_become_completed_or_follow_redirects(plugin, monkeypatch, status):
    client = Mock()
    client.get_res.return_value = SimpleNamespace(status_code=status)
    monkeypatch.setattr(module, "RequestUtils", Mock(return_value=client))
    with pytest.raises(DataError, match="HTTP"):
        plugin._read_user(site(20, "Audiences", "https://audiences.me"), 77)
    assert client.get_res.call_args.kwargs["allow_redirects"] is False


def test_transport_exceptions_do_not_expose_credentials(plugin, monkeypatch):
    factory = Mock(side_effect=RuntimeError("private-api-key private-cookie"))
    monkeypatch.setattr(module, "RequestUtils", factory)
    with pytest.raises(DataError) as captured:
        plugin._read_user(site(), 77)
    assert "private" not in str(captured.value) and "RuntimeError" in str(captured.value)


def nexus_response(amount="2.000", uid=77):
    body = (f'<h1><a href="userdetails.php?id={uid}">sample</a></h1>'
            f'<table><tr><td>上传量</td><td>{amount} TB</td></tr></table>')
    return SimpleNamespace(status_code=200, content=body.encode())


def retry_stop():
    return Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))


@pytest.mark.parametrize("site_id", ["10", "20"])
def test_retry_recovers_from_no_response_and_server_error_once_per_user(plugin, monkeypatch, site_id):
    success = (SimpleNamespace(status_code=200, json=lambda: {"code": "0", "data": {
        "id": 77, "memberCount": {"uploaded": 2 * TB}}}) if site_id == "10" else nexus_response())
    client = Mock()
    method = client.post_res if site_id == "10" else client.get_res
    method.side_effect = [None, SimpleNamespace(status_code=503), success]
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "RequestUtils", factory)
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(site_id=site_id)))
    plugin._stop = stop = retry_stop()
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert len(result["rows"]) == 1 and result["rows"][0]["status"] == "pending"
    assert result["rows"][0]["uploaded_bytes"] == 2 * TB
    assert method.call_count == 3 and [call.args[0] for call in stop.wait.call_args_list] == [5, 10]
    assert all(call.kwargs["timeout"] == 30 for call in factory.call_args_list)
    assert plugin.post_message.call_count == 1 and "【查询失败】" not in plugin.post_message.call_args.kwargs["text"]


def test_exhausted_retries_keep_previous_success_and_continue_next_user(plugin, monkeypatch):
    client = Mock()
    client.get_res.side_effect = [None, None, None, nexus_response("6.000", uid=78)]
    monkeypatch.setattr(module, "RequestUtils", Mock(return_value=client))
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(site_id="20"), **task(2, site_id="20", uid="78")))
    previous = dict(sample(), checked_at="2026-10-27 20:00:00")
    plugin.save_data("last_success", {"20:77": previous})
    plugin._stop = stop = retry_stop()
    plugin.run_check()
    rows = plugin.get_data("last_result")["rows"]
    assert [row["status"] for row in rows] == ["error", "completed"]
    assert "已尝试 3 次" in rows[0]["error"] and rows[0]["last_uploaded_bytes"] == 2 * TB
    assert plugin.get_data("last_success")["20:77"] == previous
    assert client.get_res.call_count == 4 and plugin.post_message.call_count == 1
    assert [call.args[0] for call in stop.wait.call_args_list] == [5, 10, module.REQUEST_INTERVAL]


@pytest.mark.parametrize("error", [TimeoutError("private-cookie"), ConnectionError("private-api-key")])
def test_transport_io_errors_retry_without_logging_secrets(plugin, monkeypatch, error):
    client = Mock()
    client.get_res.side_effect = [error, nexus_response()]
    monkeypatch.setattr(module, "RequestUtils", Mock(return_value=client))
    monkeypatch.setattr(module, "logger", Mock())
    stop = retry_stop()
    result = plugin._read_user_with_retry(site(20, "Audiences", "https://audiences.me"), 77, stop)
    assert result["uploaded_bytes"] == 2 * TB and client.get_res.call_count == 2
    stop.wait.assert_called_once_with(5)
    assert "private" not in repr(module.logger.mock_calls)


@pytest.mark.parametrize("response", [
    SimpleNamespace(status_code=403), SimpleNamespace(status_code=404),
    SimpleNamespace(status_code=200, content=b'<form action="takelogin.php"></form>'),
    SimpleNamespace(status_code=200, content=b'<h1>sample</h1><p>hidden</p>'),
])
def test_permanent_http_login_and_profile_errors_are_not_retried(plugin, monkeypatch, response):
    client = Mock()
    client.get_res.return_value = response
    monkeypatch.setattr(module, "RequestUtils", Mock(return_value=client))
    stop = retry_stop()
    with pytest.raises(DataError):
        plugin._read_user_with_retry(site(20, "Audiences", "https://audiences.me"), 77, stop)
    assert client.get_res.call_count == 1
    stop.wait.assert_not_called()


def test_mteam_business_auth_error_is_not_retried(plugin, monkeypatch):
    client = Mock()
    client.post_res.return_value = SimpleNamespace(status_code=200, json=lambda: {"code": "401", "message": "private-api-key"})
    monkeypatch.setattr(module, "RequestUtils", Mock(return_value=client))
    stop = retry_stop()
    with pytest.raises(DataError, match="API 拒绝"):
        plugin._read_user_with_retry(site(), 77, stop)
    assert client.post_res.call_count == 1
    stop.wait.assert_not_called()


@pytest.mark.parametrize("reenable", [False, True])
def test_stop_during_retry_wait_aborts_round_without_restarting_request(plugin, monkeypatch, reenable):
    client = Mock()
    client.get_res.return_value = None
    monkeypatch.setattr(module, "RequestUtils", Mock(return_value=client))
    config = dict(DEFAULTS, enabled=True, **task(site_id="20"))
    plugin.init_plugin(config)
    class StopOnWait(Event):
        def wait(self, timeout=None):
            plugin.init_plugin(dict(config, enabled=False))
            if reenable:
                plugin.init_plugin(config)
            return self.is_set()
    plugin._stop = old_stop = StopOnWait()
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert old_stop.is_set() and result["status"] == "stopped" and result["rows"] == []
    assert client.get_res.call_count == 1 and not plugin._lock.locked()
    plugin.post_message.assert_not_called()
    if reenable:
        assert plugin._stop is not old_stop and not plugin._stop.is_set()


@pytest.mark.parametrize("header,delay", [
    ("12", 12), ("Tue, 27 Oct 2026 21:00:20 GMT", 20), ("invalid", 5), ("7200", None),
])
def test_retry_respects_retry_after_and_defers_long_server_waits(plugin, monkeypatch, header, delay):
    client = Mock()
    client.get_res.side_effect = [SimpleNamespace(status_code=429, headers={"Retry-After": header}), nexus_response()]
    monkeypatch.setattr(module, "RequestUtils", Mock(return_value=client))
    fixed_now = datetime(2026, 10, 27, 21, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda tz: fixed_now))
    stop = retry_stop()
    selected = site(20, "Audiences", "https://audiences.me")
    if delay is None:
        with pytest.raises(DataError, match="本轮不再重试"):
            plugin._read_user_with_retry(selected, 77, stop)
        assert client.get_res.call_count == 1
        stop.wait.assert_not_called()
    else:
        assert plugin._read_user_with_retry(selected, 77, stop)["uploaded_bytes"] == 2 * TB
        stop.wait.assert_called_once_with(delay)
        assert client.get_res.call_count == 2


def test_reconfigure_keeps_current_round_snapshot(plugin):
    def reading(_site, uid):
        plugin.init_plugin(dict(DEFAULTS, enabled=True, notify=False, **task(total="10")))
        return sample(6 * TB)
    plugin._read_user = Mock(side_effect=reading)
    plugin.run_check()
    assert plugin.get_data("last_result")["rows"][0]["status"] == "completed"
    plugin.post_message.assert_called_once()


def test_stop_preserves_partial_progress_without_sending_incomplete_summary(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(), **task(2, uid="78")))
    class FastEvent(Event):
        def wait(self, timeout=None):
            return self.is_set()
    plugin._stop = FastEvent()
    def reading(_site, uid):
        plugin._stop.set()
        return sample()
    plugin._read_user = Mock(side_effect=reading)
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert result["status"] == "stopped" and len(result["rows"]) == 1
    plugin.post_message.assert_not_called()


def test_lock_blocks_overlapping_hourly_runs(plugin):
    plugin._read_user = Mock()
    plugin._lock.acquire()
    try:
        plugin.run_check()
    finally:
        plugin._lock.release()
    plugin._read_user.assert_not_called()


def test_config_and_details_keep_credentials_out_and_usernames_as_text(plugin):
    form, defaults = plugin.get_form()
    assert "onClick" in repr(form) and "task_1_site_id" in repr(form)
    assert defaults["cron"] == "0 * * * *"
    assert "private-api-key" not in repr(form) and "private-cookie" not in repr(form)
    plugin._read_user = Mock(return_value=sample(username="<script>alert(1)</script>"))
    plugin.run_check()
    page = plugin.get_page()
    assert "<script>alert(1)</script>" in repr(page)
    assert "'html'" not in repr(page)
