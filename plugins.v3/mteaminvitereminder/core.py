"""与 MoviePilot 无关的资料校验及提醒判断。"""
from datetime import datetime, timedelta
from hashlib import sha256
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


class DataError(ValueError):
    """无法可靠判断的站点数据。"""


def positive_id(value):
    """拒绝布尔值等不能可靠标识账号的输入。"""
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value)):
        raise DataError("用户 ID 缺失或无效")
    return int(value)


def boolean(value):
    """不把字符串 false 当作真值。"""
    if isinstance(value, bool):
        return value
    if value in (0, "0", "false", "no"):
        return False
    if value in (1, "1", "true", "yes"):
        return True
    raise DataError("账号状态或封存状态缺失，无法判断")


def parse_time(value):
    """无时区的站点时间按北京时间处理。"""
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise DataError("最后浏览时间缺失或格式异常") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def manual_roster(text):
    """手工 UID 名单仍须通过个人资料的 parentId 校验。"""
    rows = [{"id": positive_id(token)} for token in re.split(r"[\s,]+", str(text or "").strip()) if token]
    if not rows:
        raise DataError("手工 UID 名单为空")
    return unique_roster(rows)


def unique_roster(rows):
    """按 UID 去重，非法条目令整份名单失败。"""
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DataError("邀请名单含非对象条目")
        uid = positive_id(row.get("id"))
        result[uid] = dict(row, id=uid)
    return list(result.values())


def api_roster(data):
    """仅接受明确的名单结构；未知结构及疑似分页结果不当作完整名单。"""
    if isinstance(data, list):
        return unique_roster(data)
    if isinstance(data, dict):
        rows = None
        for key in ("members", "invitees", "data"):
            if isinstance(data.get(key), list):
                if rows is not None:
                    raise DataError("邀请响应含多个名单，需联调确认")
                rows = data[key]
        if rows is not None:
            total = data.get("total")
            if total is not None and int(total) != len(rows):
                raise DataError("邀请名单未完整返回，需适配分页后再运行")
            if int(data.get("totalPages") or 1) > 1:
                raise DataError("邀请名单存在分页，需联调适配")
            return unique_roster(rows)
        keys = ",".join(sorted(str(k) for k in data)[:15])
        raise DataError("邀请接口结构尚未适配；字段名：" + keys)
    raise DataError("邀请接口没有返回可识别名单")


def assess(profile, row, owner_id, now, threshold=30, manual=False):
    """按完整时间差判断 >30 天，仅对有可靠证据的直接下级给出待提醒。"""
    uid = positive_id(row["id"])
    if positive_id(profile.get("id")) != uid or uid == owner_id:
        raise DataError("资料 UID 不匹配或是本人")
    parent = profile.get("parentId")
    if positive_id(parent) != owner_id:
        raise DataError("无法确认该账号是本人的直接下级")
    if not boolean(profile.get("enabled")):
        return {"eligible": False, "reason": "账号已禁用"}
    try:
        role = positive_id(profile.get("role"))
    except DataError:
        raise DataError("等级缺失或异常") from None
    if role >= 7:
        return {"eligible": False, "reason": "Extreme User 或以上/特殊等级，跳过"}
    parked = boolean(profile.get("parked"))
    if parked and role >= 6:
        return {"eligible": False, "reason": "Veteran User 已封存，永久保留"}
    status = profile.get("memberStatus")
    if not isinstance(status, dict):
        raise DataError("账号浏览状态缺失或格式异常")
    last = parse_time(status.get("lastBrowse"))
    if last > now:
        raise DataError("最后浏览时间晚于当前时间")
    limit = 90 if parked else 40
    deadline = last + timedelta(days=limit)
    idle = now - last
    common = {"last_browse": last.isoformat(), "days": idle.days,
              "limit": limit, "deadline": deadline.isoformat()}
    if now >= deadline:
        return dict(common, eligible=False, reason="已达到保留期限，需人工核实账号状态")
    if idle <= timedelta(days=threshold):
        return dict(common, eligible=False, reason="未超过提醒阈值")
    return dict(common, eligible=True, reason="待提醒")


def delivery_key(owner_id, uid, result):
    """以本人、下级和最后浏览时间识别一次未登录周期。"""
    source = f"{owner_id}|{uid}|{result['last_browse']}"
    return sha256(source.encode()).hexdigest()
