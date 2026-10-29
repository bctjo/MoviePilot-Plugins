"""核心字节规则、模板解析和完整通知；页面为合成样例，不含真实用户资料。"""
import pytest

from app.plugins.uploadprogressmonitor import DEFAULTS
from app.plugins.uploadprogressmonitor.core import (DataError, UNITS, format_bytes, notification_texts,
    parse_mteam_profile, parse_nexus_profile, parse_tasks, progress, amount_bytes)

TB = UNITS["TB"]


def config_task(slot=1, **overrides):
    task = {"visible": True, "enabled": True, "site_id": "10", "uid": "77", "initial": "0", "initial_unit": "TB", "total": "5", "total_unit": "TB", "label": "客户 A"}
    task.update(overrides)
    return {f"task_{slot}_{key}": value for key, value in task.items()}


def nexus_page(amount="5.00 TB", exact=None, traditional=False):
    heading = '<h1><a href="userdetails.php?id=77">sample-user</a></h1>'
    title = f' title="{exact:,} bytes"' if exact is not None else ""
    upload = "上傳量" if traditional else "上传量"
    return ('<html><body><div id="nav"><strong>上传量</strong>: 99.00 TB</div>' + heading +
            f'<table><tr><td class="rowhead">传输</td><td><table><tr><td><strong>{upload}</strong>: '
            f'<span{title}>{amount}</span></td><td><strong>下载量</strong>: 1.00 TB</td></tr>'
            '<tr><td><strong>真实上传量</strong>: 80.00 TB</td></tr></table></td></tr></table></body></html>')


def audiences_page(amount="6.125 TB", exact=None):
    """按观众卡片布局重建的合成页面，不复制原始用户资料。"""
    title = f' title="{exact} bytes"' if exact is not None else ""
    return ('<div id="nav"><a href="userdetails.php?id=77">登录账号</a>上传量 99 TB</div>'
            '<table><tr><td class="embedded"><div class="ud-page">'
            '<div class="ud-hero"><div class="ud-hero__main"><div class="ud-hero__name">'
            '<span class="nowrap"><a href="userdetails.php?id=77"><b>card-user</b></a></span>'
            '<span class="ud-flag"><img alt="国旗"></span></div></div></div>'
            '<div class="ud-stats"><div class="ud-stat"><div class="ud-stat__body">'
            '<div class="ud-stat__value">90.000 TB</div><div class="ud-stat__label">上传</div></div></div></div>'
            '<div class="ud-grid"><section class="ud-card"><h2 class="ud-card__title">账号信息</h2>'
            '<div class="ud-card__body"><div class="ud-row"><div class="ud-row__label">UID</div>'
            '<div class="ud-row__value">77</div></div><div class="ud-row"><div class="ud-row__label">邀请人</div>'
            '<div class="ud-row__value"><a href="userdetails.php?id=88">inviter</a></div></div></div></section>'
            '<section class="ud-card"><h2 class="ud-card__title">传输与时间</h2><div class="ud-card__body">'
            '<div class="ud-row"><div class="ud-row__label"><i class="fas fa-arrow-up"></i><span>上传量</span></div>'
            f'<div class="ud-row__value"{title}>{amount}</div></div>'
            '<div class="ud-row"><div class="ud-row__label">真实上传量</div><div class="ud-row__value">80 TB</div></div>'
            '</div></section></div></div></td></tr></table>')


@pytest.mark.parametrize("target,expected", [("5", 5 * TB), ("0.5", TB // 2), ("0.000000000001", 2)])
def test_target_uses_binary_units_and_rounds_up_to_a_byte(target, expected):
    assert amount_bytes(target, "TB") == expected


def test_target_cannot_round_away_a_positive_fraction_of_a_byte():
    assert amount_bytes("5.000000000000000000000000000001", "TB") == 5 * TB + 1
    assert amount_bytes("1e-1000000", "TB") == 1
    with pytest.raises(DataError):
        amount_bytes("1e999999", "TB")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "abc", None, True])
def test_invalid_targets_rejected(value):
    with pytest.raises(DataError):
        amount_bytes(value, "TB")


def test_each_uid_can_bind_independent_sites_and_duplicate_pairs_rejected():
    config = dict(config_task(), **config_task(2, site_id="20"))
    tasks = parse_tasks(config)
    assert [task["key"] for task in tasks] == ["10:77", "20:77"]
    config["task_2_site_id"] = "10"
    with pytest.raises(DataError, match="重复任务"):
        parse_tasks(config)


def test_empty_removed_or_paused_slots_ignored():
    assert parse_tasks(DEFAULTS) == []
    assert parse_tasks(config_task(enabled=False, initial="bad")) == []
    assert parse_tasks(config_task(visible=False, initial="bad")) == []


def test_partial_task_requires_uid_site_and_goal():
    with pytest.raises(DataError, match="任务 1"):
        parse_tasks(config_task(site_id=""))


def test_initial_upload_and_task_total_add_to_target_with_independent_units():
    task = parse_tasks(config_task(initial="3", total="2048", total_unit="GB"))[0]
    assert task["target_bytes"] == 5 * TB
    assert task["total_bytes"] == 2 * TB and task["initial_bytes"] == 3 * TB
    task = parse_tasks(config_task(initial=0, total="5"))[0]
    assert task["initial_bytes"] == 0
    task = parse_tasks(config_task(initial="1", total="6"))[0]
    assert task["target_bytes"] == 7 * TB


@pytest.mark.parametrize("initial", [None, "", "  ", "-1", "NaN", "Infinity", "abc", True])
def test_initial_upload_is_required_and_nonnegative(initial):
    with pytest.raises(DataError, match="任务 1：初始上传量"):
        parse_tasks(config_task(initial=initial))


@pytest.mark.parametrize("total", [None, "", "  ", "0", "-1", "NaN", "Infinity", "abc", True])
def test_task_total_is_required_and_positive(total):
    with pytest.raises(DataError, match="任务 1：任务总量"):
        parse_tasks(config_task(total=total))


def test_combined_target_cannot_overflow_and_units_are_gb_tb_pb():
    with pytest.raises(DataError, match="相加超过"):
        parse_tasks(config_task(initial="16777215", total="1"))
    with pytest.raises(DataError, match="有效单位"):
        parse_tasks(config_task(initial_unit="TiB"))
    assert parse_tasks(config_task(initial="1024", initial_unit="GB", total="1", total_unit="PB"))[0]["target_bytes"] == TB + 1024 * TB


def test_task_total_alone_is_incomplete_instead_of_silently_skipped():
    with pytest.raises(DataError, match="任务 1"):
        parse_tasks({"task_1_total": "2"})


def test_mteam_reads_requested_member_total_and_keeps_large_integer_precision():
    count = 18_446_744_073_709_551_615
    parsed = parse_mteam_profile({"id": "77", "username": "sample", "memberCount": {"uploaded": str(count)}}, 77)
    assert parsed["uploaded_bytes"] == count
    assert parse_mteam_profile({"id": 77, "memberCount": {"uploaded": "0"}}, 77)["uploaded_bytes"] == 0


@pytest.mark.parametrize("value", [None, "5 TB", -1, 1.5, True])
def test_mteam_missing_or_invalid_counter_is_not_zero(value):
    with pytest.raises(DataError):
        parse_mteam_profile({"id": 77, "memberCount": {"uploaded": value}}, 77)


def test_mteam_cannot_substitute_a_different_user():
    with pytest.raises(DataError):
        parse_mteam_profile({"id": 78, "memberCount": {"uploaded": 9 * TB}}, 77)


def test_nexus_reads_target_not_navbar_or_real_upload():
    parsed = parse_nexus_profile(nexus_page(exact=5 * TB), 77)
    assert parsed["username"] == "sample-user"
    assert parsed["uploaded_bytes"] == 5 * TB
    assert progress(parsed, 0, 5 * TB)["status"] == "completed"


@pytest.mark.parametrize("traditional", [False, True])
def test_formatted_nexus_upload_matching_goal_is_completed(traditional):
    sample = parse_nexus_profile(nexus_page(traditional=traditional), 77)
    state = progress(sample, 0, 5 * TB)
    assert state["status"] == "completed" and state["percent"] == 100
    assert state["remaining_bytes"] == 0
    assert progress(parse_nexus_profile(nexus_page("5.01 TB", traditional=traditional), 77), 0, 5 * TB)["status"] == "completed"


def test_nexus_separate_cells_and_english_labels():
    source = '<h1>sample</h1><table><tr><td>Uploaded</td><td>2,048 GB</td></tr></table>'
    assert parse_nexus_profile(source, 77)["uploaded_bytes"] == 2 * TB


@pytest.mark.parametrize("source", [
    '<div><strong>上传量</strong>: 99 TB</div><h1>sample</h1><p>隐私保护</p>',
    '<h1>sample</h1><table><tr><td><strong>真实上传量</strong>: 99 TB</td></tr></table>',
    '<h1>登录</h1><form action="takelogin.php"></form>',
    '<h1>sample</h1><table><tr><td>上传量</td><td>资料隐藏</td></tr></table>',
])
def test_nexus_missing_user_counter_fails_instead_of_using_other_statistics(source):
    with pytest.raises(DataError):
        parse_nexus_profile(source, 77)


def test_nexus_identity_and_conflicting_counters_rejected():
    with pytest.raises(DataError, match="UID"):
        parse_nexus_profile(nexus_page().replace("id=77", "id=78"), 77)
    source = nexus_page() + '<table><tr><td>上传量</td><td>10.00 TB</td></tr></table>'
    with pytest.raises(DataError, match="多个"):
        parse_nexus_profile(source, 77)


def test_nexus_ambiguous_banner_and_login_page_cannot_supply_user_upload():
    with pytest.raises(DataError):
        parse_nexus_profile('<h1>站点</h1><table><tr><td>上传量</td><td>99 TB</td></tr></table><h1>私密资料</h1>', 77)
    with pytest.raises(DataError, match="登录页"):
        parse_nexus_profile(nexus_page() + '<form action="takelogin.php"></form>', 77)
    assert parse_nexus_profile('<h1>站点</h1>' + nexus_page(exact=5 * TB), 77)["uploaded_bytes"] == 5 * TB


def test_audiences_card_reads_upload_row_and_name_without_h1():
    source = audiences_page() + '<table><tr><td>上传量</td><td>100 TB</td></tr></table>'
    sample = parse_nexus_profile(source, 77)
    assert sample["username"] == "card-user"
    assert sample["uploaded_bytes"] == 6 * TB + TB // 8
    assert progress(sample, 0, 5 * TB)["status"] == "completed"


def test_audiences_card_uses_visible_upload_without_deducting_margin():
    sample = parse_nexus_profile(audiences_page("5.000 TB"), 77)
    assert progress(sample, 0, 5 * TB)["status"] == "completed"
    sample = parse_nexus_profile(audiences_page("5.000 TB", exact=5 * TB - 1), 77)
    assert sample["uploaded_bytes"] == 5 * TB
    assert progress(sample, 0, 5 * TB)["status"] == "completed"


@pytest.mark.parametrize("old,new", [
    ('<a href="userdetails.php?id=77"><b>', '<a href="userdetails.php?id=78"><b>'),
    ('<a href="userdetails.php?id=77"><b>', '<a href="userdetails.php?id=77&amp;id=78"><b>'),
    ('<div class="ud-row__value">77</div>', '<div class="ud-row__value">78</div>'),
    ('<div class="ud-row__label">UID</div>', '<div class="ud-row__label">隐藏</div>'),
])
def test_audiences_card_checks_both_profile_link_and_uid_row(old, new):
    with pytest.raises(DataError, match="UID"):
        parse_nexus_profile(audiences_page().replace(old, new), 77)


@pytest.mark.parametrize("source", [
    audiences_page("资料隐藏"),
    audiences_page().replace('<span>上传量</span>', '<span>真实上传量</span>'),
])
def test_audiences_private_or_missing_upload_cannot_use_navbar_or_summary(source):
    with pytest.raises(DataError, match="未找到目标用户上传量"):
        parse_nexus_profile(source, 77)


def test_audiences_multiple_cards_and_conflicting_upload_rows_rejected():
    with pytest.raises(DataError, match="唯一定位"):
        parse_nexus_profile(audiences_page() * 2, 77)
    source = audiences_page().replace('<div class="ud-row__label">真实上传量</div>',
                                     '<div class="ud-row__label">上传量</div>')
    with pytest.raises(DataError, match="多个不同"):
        parse_nexus_profile(source, 77)


def test_nexus_login_form_without_heading_reports_login_failure():
    with pytest.raises(DataError, match="登录页"):
        parse_nexus_profile('<form action="takelogin.php"><input name="username"></form>', 77)


def test_threshold_uses_current_total_not_growth_since_start():
    sample = {"uploaded_bytes": 5 * TB}
    assert progress(sample, 2 * TB, 3 * TB)["status"] == "completed"
    assert progress(sample, 2 * TB, 4 * TB)["status"] == "pending"
    assert format_bytes(5 * TB) == "5.00 TB"


@pytest.mark.parametrize("current,percent,done,status", [
    (39, 0, 0, "pending"), (40, 0, 0, "pending"), (44, 40, 4, "pending"),
    (50, 100, 10, "completed"), (55, 100, 10, "completed"),
])
def test_progress_uses_task_total_instead_of_account_lifetime_total(current, percent, done, status):
    sample = {"uploaded_bytes": current * TB}
    result = progress(sample, 40 * TB, 10 * TB)
    assert result["percent"] == percent and result["status"] == status
    assert result["task_uploaded_bytes"] == done * TB
    assert result["remaining_bytes"] == max(0, (50 - current) * TB)


@pytest.mark.parametrize("initial,unit,total,displayed", [
    ("0.3", "TB", "5", "5.300 TB"),
    ("0.1", "TB", "0.2", "0.300 TB"),
    ("307.2", "GB", "5", "5.300 TB"),
])
def test_visible_upload_equal_to_decimal_goal_has_no_artificial_remainder(initial, unit, total, displayed):
    task = parse_tasks(config_task(initial=initial, initial_unit=unit, total=total))[0]
    sample = parse_nexus_profile(audiences_page(displayed), 77)
    result = progress(sample, task["initial_bytes"], task["total_bytes"], task["target_bytes"])
    assert sample["uploaded_bytes"] == task["target_bytes"]
    assert result["status"] == "completed" and result["percent"] == 100
    assert result["remaining_bytes"] == 0 and result["task_uploaded_bytes"] == task["total_bytes"]


@pytest.mark.parametrize("displayed,status", [("5.299 TB", "pending"), ("5.300 TB", "completed"), ("5.301 TB", "completed")])
def test_completion_compares_the_site_value_against_goal(displayed, status):
    task = parse_tasks(config_task(initial="0.3", total="5"))[0]
    sample = parse_nexus_profile(audiences_page(displayed), 77)
    result = progress(sample, task["initial_bytes"], task["total_bytes"], task["target_bytes"])
    assert result["status"] == status
    assert (result["remaining_bytes"] > 0) == (status == "pending")


def test_exact_api_counter_still_requires_reaching_target_byte():
    below = progress({"uploaded_bytes": 5 * TB - 1}, 0, 5 * TB)
    reached = progress({"uploaded_bytes": 5 * TB}, 0, 5 * TB)
    assert below["status"] == "pending" and below["remaining_bytes"] == 1
    assert reached["status"] == "completed" and reached["remaining_bytes"] == 0


def test_task_notification_shows_initial_total_and_completed_volume():
    sample = {"uploaded_bytes": 44 * TB}
    row = dict(parse_tasks(config_task(initial="40", total="10"))[0], **sample, **progress(sample, 40 * TB, 10 * TB),
               site_name="站点")
    message = notification_texts({"rows": [row], "updated_at": "2026-10-27 10:00:00"})[0]
    assert "目标上传量：50.00 TB" in message
    assert "任务总量：10.00 TB · 初始：40.00 TB" in message
    assert "本次已完成：4.00 TB" in message and "进度：40.0%" in message


def test_grouped_notifications_keep_every_task_across_batches():
    rows = []
    for uid in range(1, 101):
        row = {"uid": uid, "site_name": "站点", "label": f"客户-{uid:03}", "target_bytes": 5 * TB,
               "initial_bytes": 0, "total_bytes": 5 * TB, "task_uploaded_bytes": 2 * TB,
               "status": "error" if uid % 3 == 0 else "completed" if uid % 3 == 1 else "pending",
               "error": "Cookie 失效", "uploaded_bytes": 2 * TB,
               "percent": 40, "remaining_bytes": 3 * TB}
        rows.append(row)
    pages = notification_texts({"rows": rows, "updated_at": "2026-10-27 10:00:00"}, limit=1000)
    assert len(pages) > 1
    for uid in range(1, 101):
        assert "\n\n".join(pages).count(f"客户-{uid:03}（") == 1
    assert all(len(page) <= 1100 for page in pages)
    assert "【待完成】" in pages[0] and "【已完成】" in "".join(pages) and "【查询失败】" in "".join(pages)
