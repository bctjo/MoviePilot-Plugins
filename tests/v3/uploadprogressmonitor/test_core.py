"""核心字节规则、模板解析和完整通知；页面为合成样例，不含真实用户资料。"""
import pytest

from app.plugins.uploadprogressmonitor import DEFAULTS
from app.plugins.uploadprogressmonitor.core import (DataError, UNITS, format_bytes, notification_texts,
    parse_mteam_profile, parse_nexus_profile, parse_tasks, progress, target_bytes)

TIB = UNITS["TiB"]


def config_task(slot=1, **overrides):
    task = {"visible": True, "enabled": True, "site_id": "10", "uid": "77", "target": "5", "unit": "TiB", "label": "客户 A"}
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


@pytest.mark.parametrize("target,expected", [("5", 5 * TIB), ("0.5", TIB // 2), ("0.000000000001", 2)])
def test_target_uses_binary_units_and_rounds_up_to_a_byte(target, expected):
    assert target_bytes(target, "TiB") == expected


def test_target_cannot_round_away_a_positive_fraction_of_a_byte():
    assert target_bytes("5.000000000000000000000000000001", "TiB") == 5 * TIB + 1
    assert target_bytes("1e-1000000", "TiB") == 1
    with pytest.raises(DataError):
        target_bytes("1e999999", "TiB")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "abc", None, True])
def test_invalid_targets_rejected(value):
    with pytest.raises(DataError):
        target_bytes(value, "TiB")


def test_each_uid_can_bind_independent_sites_and_duplicate_pairs_rejected():
    config = dict(config_task(), **config_task(2, site_id="20"))
    tasks = parse_tasks(config)
    assert [task["key"] for task in tasks] == ["10:77", "20:77"]
    config["task_2_site_id"] = "10"
    with pytest.raises(DataError, match="重复任务"):
        parse_tasks(config)


def test_empty_removed_or_paused_slots_ignored():
    assert parse_tasks(DEFAULTS) == []
    assert parse_tasks(config_task(enabled=False, target="bad")) == []
    assert parse_tasks(config_task(visible=False, target="bad")) == []


def test_partial_task_requires_uid_site_and_goal():
    with pytest.raises(DataError, match="任务 1"):
        parse_tasks(config_task(site_id=""))


def test_mteam_reads_requested_member_total_and_keeps_large_integer_precision():
    count = 18_446_744_073_709_551_615
    parsed = parse_mteam_profile({"id": "77", "username": "sample", "memberCount": {"uploaded": str(count)}}, 77)
    assert parsed["uploaded_bytes"] == count and not parsed["approximate"]
    assert parsed["lower_bound_bytes"] == count
    assert parse_mteam_profile({"id": 77, "memberCount": {"uploaded": "0"}}, 77)["uploaded_bytes"] == 0


@pytest.mark.parametrize("value", [None, "5 TB", -1, 1.5, True])
def test_mteam_missing_or_invalid_counter_is_not_zero(value):
    with pytest.raises(DataError):
        parse_mteam_profile({"id": 77, "memberCount": {"uploaded": value}}, 77)


def test_mteam_cannot_substitute_a_different_user():
    with pytest.raises(DataError):
        parse_mteam_profile({"id": 78, "memberCount": {"uploaded": 9 * TIB}}, 77)


def test_nexus_reads_target_not_navbar_or_real_upload():
    parsed = parse_nexus_profile(nexus_page(exact=5 * TIB), 77)
    assert parsed["username"] == "sample-user"
    assert parsed["uploaded_bytes"] == 5 * TIB and not parsed["approximate"]
    assert progress(parsed, 5 * TIB)["status"] == "completed"


@pytest.mark.parametrize("traditional", [False, True])
def test_formatted_nexus_boundary_is_pending_until_precision_confirms(traditional):
    sample = parse_nexus_profile(nexus_page(traditional=traditional), 77)
    assert sample["approximate"]
    state = progress(sample, 5 * TIB)
    assert state["status"] == "pending" and state["precision_note"]
    assert progress(parse_nexus_profile(nexus_page("5.01 TiB", traditional=traditional), 77), 5 * TIB)["status"] == "completed"


def test_nexus_separate_cells_and_english_labels():
    source = '<h1>sample</h1><table><tr><td>Uploaded</td><td>2,048 GiB</td></tr></table>'
    assert parse_nexus_profile(source, 77)["uploaded_bytes"] == 2 * TIB


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
    assert parse_nexus_profile('<h1>站点</h1>' + nexus_page(exact=5 * TIB), 77)["uploaded_bytes"] == 5 * TIB


def test_audiences_card_reads_upload_row_and_name_without_h1():
    source = audiences_page() + '<table><tr><td>上传量</td><td>100 TB</td></tr></table>'
    sample = parse_nexus_profile(source, 77)
    assert sample["username"] == "card-user"
    assert sample["uploaded_bytes"] == 6 * TIB + TIB // 8
    assert sample["approximate"] and progress(sample, 5 * TIB)["status"] == "completed"


def test_audiences_card_preserves_rounding_and_exact_byte_behavior():
    sample = parse_nexus_profile(audiences_page("5.000 TB"), 77)
    assert progress(sample, 5 * TIB)["status"] == "pending"
    sample = parse_nexus_profile(audiences_page("5.000 TB", exact=5 * TIB), 77)
    assert sample["uploaded_bytes"] == 5 * TIB and not sample["approximate"]
    assert progress(sample, 5 * TIB)["status"] == "completed"


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
    sample = {"uploaded_bytes": 5 * TIB, "lower_bound_bytes": 5 * TIB, "approximate": False}
    assert progress(sample, 5 * TIB)["status"] == "completed"
    assert progress(sample, 6 * TIB)["status"] == "pending"
    assert format_bytes(5 * TIB) == "5.00 TiB"


def test_grouped_notifications_keep_every_task_across_batches():
    rows = []
    for uid in range(1, 101):
        row = {"uid": uid, "site_name": "站点", "label": f"客户-{uid:03}", "target_bytes": 5 * TIB,
               "status": "error" if uid % 3 == 0 else "completed" if uid % 3 == 1 else "pending",
               "error": "Cookie 失效", "uploaded_bytes": 2 * TIB, "approximate": False,
               "percent": 40, "remaining_bytes": 3 * TIB}
        rows.append(row)
    pages = notification_texts({"rows": rows, "updated_at": "2026-10-27 10:00:00"}, limit=1000)
    assert len(pages) > 1
    for uid in range(1, 101):
        assert "\n\n".join(pages).count(f"客户-{uid:03}（") == 1
    assert all(len(page) <= 1100 for page in pages)
    assert "【待完成】" in pages[0] and "【已完成】" in "".join(pages) and "【查询失败】" in "".join(pages)
