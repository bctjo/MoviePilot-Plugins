"""宿主、HTTP、调度和通知边界均使用替身，不访问真实客户或站点。"""
from copy import deepcopy
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.plugins.uploadprogressmonitor as module
from app.plugins.uploadprogressmonitor import DEFAULTS, UploadProgressMonitor
from app.plugins.uploadprogressmonitor.core import DataError, UNITS

TIB = UNITS["TiB"]


def task(slot=1, **overrides):
    values = {"visible": True, "enabled": True, "site_id": "10", "uid": "77", "target": "5", "unit": "TiB", "label": "客户 A"}
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


def sample(uploaded=2 * TIB, username="sample"):
    return {"username": username, "uploaded_bytes": uploaded, "lower_bound_bytes": uploaded, "approximate": False}


def test_default_schedule_is_hourly_and_once_uses_host_scheduler(plugin):
    assert DEFAULTS["cron"] == "0 * * * *" and DEFAULTS["notify"]
    assert str(plugin.get_service()[0]["trigger"]) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='0']"
    plugin.init_plugin(dict(DEFAULTS, enabled=True, onlyonce=True, **task()))
    module.scheduler_sdk.add_plugin_once_job.assert_called_once()
    assert plugin.update_config.call_args.args[0]["onlyonce"] is False


def test_invalid_configuration_and_failed_once_scheduling_do_not_start_network(plugin):
    plugin._read_user = Mock()
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(target="bad")))
    assert not plugin.get_state() and plugin.get_service() == []
    plugin.run_check()
    plugin._read_user.assert_not_called()
    module.scheduler_sdk.add_plugin_once_job.side_effect = RuntimeError("private-cookie")
    plugin.init_plugin(dict(DEFAULTS, enabled=True, onlyonce=True, **task()))
    assert plugin.get_state() and plugin._notice and "private" not in plugin._notice


def test_all_statuses_are_sent_every_round_and_completed_tasks_keep_refreshing(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(), **task(2, uid="78")))
    plugin._read_user = Mock(side_effect=[sample(2 * TIB), sample(5 * TIB)] * 2)
    plugin.run_check()
    plugin.run_check()
    assert plugin._read_user.call_count == 4 and plugin.post_message.call_count == 2
    text = plugin.post_message.call_args.kwargs["text"]
    assert "【待完成】" in text and "【已完成】" in text
    assert "当前上传量：2.00 TiB" in text and "目标上传量：5.00 TiB" in text


def test_same_uid_different_site_uses_each_tasks_site_credentials(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(), **task(2, site_id="20")))
    plugin._read_user = Mock(return_value=sample())
    plugin.run_check()
    calls = plugin._read_user.call_args_list
    assert [(call.args[0].id, call.args[1]) for call in calls] == [(10, 77), (20, 77)]


def test_failed_user_is_separate_from_completed_and_other_users_continue(plugin):
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(), **task(2, uid="78")))
    plugin._read_user = Mock(side_effect=[RuntimeError("private-cookie"), sample(6 * TIB)])
    plugin.run_check()
    result = plugin.get_data("last_result")
    assert [row["status"] for row in result["rows"]] == ["error", "completed"]
    assert "private-cookie" not in repr(result)
    assert "【查询失败】" in plugin.post_message.call_args.kwargs["text"]


def test_failed_refresh_shows_last_good_value_but_never_completed_status(plugin):
    plugin._read_user = Mock(return_value=sample(6 * TIB))
    plugin.run_check()
    plugin._read_user.side_effect = DataError("Cookie 失效")
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["status"] == "error" and row["last_uploaded_bytes"] == 6 * TIB
    assert "【已完成】" not in plugin.post_message.call_args.kwargs["text"]
    assert "上次成功" in plugin.post_message.call_args.kwargs["text"]


def test_counter_decrease_reverts_completion_and_warns(plugin):
    plugin._read_user = Mock(side_effect=[sample(6 * TIB), sample(TIB)])
    plugin.run_check()
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["status"] == "pending" and row["delta_bytes"] == -5 * TIB
    assert row["warning"] and row["completed_at"] is None


def test_editing_goal_recomputes_completion_without_old_delivery_state(plugin):
    plugin._read_user = Mock(return_value=sample(6 * TIB))
    plugin.run_check()
    plugin.init_plugin(dict(DEFAULTS, enabled=True, **task(target="10")))
    plugin.run_check()
    row = plugin.get_data("last_result")["rows"][0]
    assert row["target_bytes"] == 10 * TIB and row["status"] == "pending"
    assert row["completed_at"] is None


def test_deleted_site_does_not_query_another_site(plugin, monkeypatch):
    monkeypatch.setattr(module, "SiteOper", Mock(return_value=SimpleNamespace(list_order_by_pri=lambda: [])))
    plugin._read_user = Mock()
    plugin.run_check()
    plugin._read_user.assert_not_called()
    assert plugin.get_data("last_result")["rows"][0]["status"] == "error"


def test_mteam_request_has_api_key_and_never_cookie_or_redirect(plugin, monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {"code": "0", "data": {"id": 77, "username": "sample", "memberCount": {"uploaded": str(5 * TIB)}}})
    client = Mock()
    client.post_res.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "RequestUtils", factory)
    assert plugin._read_user(site(), 77)["uploaded_bytes"] == 5 * TIB
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
    assert result["uploaded_bytes"] < 6 * TIB
    assert factory.call_args.kwargs["cookies"] == "private-cookie"
    assert "x-api-key" not in factory.call_args.kwargs["headers"]
    client.get_res.assert_called_once_with("https://audiences.me/userdetails.php", params={"id": 77}, allow_redirects=False)


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


def test_reconfigure_keeps_current_round_snapshot(plugin):
    def reading(_site, uid):
        plugin.init_plugin(dict(DEFAULTS, enabled=True, notify=False, **task(target="10")))
        return sample(6 * TIB)
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
