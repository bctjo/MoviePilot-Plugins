"""上传量任务、字节换算、站点资料解析及通知排版，不访问网络。"""
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, ROUND_CEILING, localcontext
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
MAX_TASKS = 100
UNITS = {name: 1024 ** power for power, name in enumerate(("B", "KB", "MB", "GB", "TB", "PB", "EB"))}
TASK_UNITS = ("GB", "TB", "PB")
UPLOAD_LABELS = {"上传", "上传量", "已上传", "上傳", "上傳量", "已上傳", "uploaded", "upload"}


class DataError(ValueError):
    """配置或站点资料不足以可靠判断进度。"""


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise DataError("UID 和站点 ID 必须为正整数")
    return int(value)


def label(value):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))).strip()[:80]


def amount_bytes(value, unit, name="任务总量", allow_zero=False):
    try:
        number = Decimal(str(value))
        if (isinstance(value, bool) or not number.is_finite() or number < 0
                or (number == 0 and not allow_zero) or unit not in TASK_UNITS):
            raise ValueError
        if number > Decimal(2 ** 64 - 1) / UNITS[unit] or len(number.as_tuple().digits) > 128:
            raise ValueError
        with localcontext() as context:
            context.prec = max(64, len(number.as_tuple().digits) + 20)
            return 0 if number == 0 else max(1, int((number * UNITS[unit]).to_integral_value(rounding=ROUND_CEILING)))
    except (DecimalException, ValueError, TypeError, OverflowError):
        requirement = "大于或等于 0" if allow_zero else "大于 0"
        raise DataError(f"{name}必须为{requirement}的数值，并选择有效单位") from None


def task_defaults():
    """固定字段供宿主 JSON 表单使用，空白任务不参与扫描。"""
    result = {"open_panels": [1]}
    for index in range(1, MAX_TASKS + 1):
        for key, value in {"visible": index == 1, "enabled": True, "site_id": "", "uid": "",
                           "initial": "", "initial_unit": "TB", "total": "", "total_unit": "TB", "label": ""}.items():
            result[f"task_{index}_{key}"] = value
    return result


def parse_tasks(config):
    tasks, seen = [], set()
    for index in range(1, MAX_TASKS + 1):
        get = lambda key, default=None: config.get(f"task_{index}_{key}", default)
        if not get("visible", index == 1) or not get("enabled", True):
            continue
        uid, site_id, initial = get("uid", ""), get("site_id", ""), get("initial", "")
        total = get("total", "")
        if all(value is None or str(value).strip() == "" for value in (uid, site_id, initial, total)):
            continue
        try:
            uid, site_id = positive_id(uid), positive_id(site_id)
            initial_value, total_value = initial, total
            initial = amount_bytes(initial, get("initial_unit", "TB"), "初始上传量", allow_zero=True)
            total = amount_bytes(total, get("total_unit", "TB"))
            # 先按原始输入相加再取整，避免两个小数分别取整后多出 1 字节。
            with localcontext() as context:
                context.prec = 160
                configured_goal = (Decimal(str(initial_value)) * UNITS[get("initial_unit", "TB")]
                                   + Decimal(str(total_value)) * UNITS[get("total_unit", "TB")])
                goal = max(initial + 1, int(configured_goal.to_integral_value(rounding=ROUND_CEILING)))
            if goal > 2 ** 64 - 1:
                raise DataError("初始上传量与任务总量相加超过支持范围")
        except DataError as error:
            raise DataError(f"任务 {index}：{error}") from None
        key = f"{site_id}:{uid}"
        if key in seen:
            raise DataError(f"任务 {index}：同一站点和 UID 已存在，请合并重复任务")
        seen.add(key)
        tasks.append({"key": key, "slot": index, "uid": uid, "site_id": site_id,
                      "target_bytes": goal, "total_bytes": total,
                      "initial_bytes": initial, "label": label(get("label"))})
    return tasks


def format_bytes(value):
    if value is None:
        return "—"
    number = Decimal(value)
    unit = "B"
    for candidate in UNITS:
        if abs(number) >= UNITS[candidate]:
            unit = candidate
    return f"{number / UNITS[unit]:,.2f} {unit}"


def exact_bytes(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise DataError("上传量缺失或不是有效的非负整数字节数")
    return int(value)


def parse_mteam_profile(data, uid):
    """只读取指定用户的累计记账上传量，忽略其它个人资料。"""
    if not isinstance(data, dict) or positive_id(data.get("id")) != uid:
        raise DataError("站点返回的用户 UID 与监控任务不一致")
    count = data.get("memberCount")
    uploaded = exact_bytes(count.get("uploaded") if isinstance(count, dict) else None)
    return {"username": label(data.get("username")) or str(uid), "uploaded_bytes": uploaded}


@dataclass
class Node:
    tag: str
    attrs: dict
    position: int
    parent: object = None
    children: list = field(default_factory=list)


class Document(HTMLParser):
    """保留节点关系，避免把登录用户的导航栏上传量当成目标用户资料。"""
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {}, 0)
        self.stack, self.nodes = [self.root], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), len(self.nodes) + 1, self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, text):
        self.stack[-1].children.append(text)


def text(node):
    if isinstance(node, str):
        return node
    if node.tag in {"script", "style"}:
        return ""
    return "".join(text(child) for child in node.children)


def descendants(node):
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from descendants(child)


def normalized(value):
    return re.sub(r"[\s:：]+", "", value).casefold()


def parse_displayed_size(value):
    match = re.match(r"\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(bytes|[KMGTPE]?i?B|字节|位元組)\b", value, re.I)
    if not match:
        raise DataError("未识别到上传量数值和单位")
    raw_number, raw_unit = match.groups()
    raw_number = raw_number.replace(",", "")
    number = Decimal(raw_number)
    raw_unit = raw_unit.upper()
    power = {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}.get(raw_unit[0], 0)
    factor = 1024 ** power
    return int((number * factor).to_integral_value(rounding=ROUND_CEILING))


def _unique_nexus_sample(samples):
    if not samples:
        raise DataError("未找到目标用户上传量，可能为隐私限制、Cookie 失效或页面改版")
    if len({item["uploaded_bytes"] for item in samples}) != 1:
        raise DataError("页面包含多个不同的上传量，无法可靠判断")
    return samples[0]


def _has_class(node, name):
    return name in node.attrs.get("class", "").split()


def _parse_nexus_card(pages, uid):
    """观众卡片布局：限定资料容器，并同时核对姓名链接与 UID 字段。"""
    if len(pages) != 1:
        raise DataError("无法唯一定位目标用户资料卡片")
    nodes = list(descendants(pages[0]))
    names = [node for node in nodes if _has_class(node, "ud-hero__name")]
    if len(names) != 1:
        raise DataError("无法唯一定位用户资料卡片姓名")
    links = [node for node in descendants(names[0]) if node.tag == "a"
             and urlparse(node.attrs.get("href", "")).path.rsplit("/", 1)[-1] == "userdetails.php"]
    if len(links) != 1 or parse_qs(urlparse(links[0].attrs["href"]).query).get("id") != [str(uid)]:
        raise DataError("页面资料 UID 与监控任务不一致或缺失")
    fields = []
    for row in nodes:
        if not _has_class(row, "ud-row"):
            continue
        children = [child for child in row.children if isinstance(child, Node)]
        labels = [child for child in children if _has_class(child, "ud-row__label")]
        values = [child for child in children if _has_class(child, "ud-row__value")]
        if len(labels) == len(values) == 1:
            fields.append((normalized(text(labels[0])), values[0]))
    identities = [text(value).strip() for name, value in fields if name == "uid"]
    if identities != [str(uid)]:
        raise DataError("资料卡片 UID 字段与监控任务不一致或缺失")
    samples = []
    for name, value in fields:
        if name in UPLOAD_LABELS:
            try:
                samples.append({"uploaded_bytes": parse_displayed_size(text(value).strip())})
            except DataError:
                continue
    return dict(_unique_nexus_sample(samples), username=label(text(links[0])) or str(uid))


def parse_nexus_profile(source, uid):
    """解析 NexusPHP 资料页，排除导航栏和真实上传等不同记账指标。"""
    if not isinstance(source, str) or len(source) > 5_000_000:
        raise DataError("用户详情页为空或超过解析大小限制")
    document = Document(source)
    if any(node.tag == "form" and urlparse(node.attrs.get("action", "")).path.rsplit("/", 1)[-1] in {"login.php", "takelogin.php"}
           for node in document.nodes):
        raise DataError("站点返回登录页，请更新 MP 站点 Cookie")
    cards = [node for node in document.nodes if _has_class(node, "ud-page")]
    if cards:
        return _parse_nexus_card(cards, uid)
    headings = [node for node in document.nodes if node.tag == "h1"]
    if not headings:
        raise DataError("未找到用户资料标题，请检查 Cookie、登录状态或页面布局")
    matching = [heading for heading in headings if any(
        node.tag == "a" and "userdetails.php" in node.attrs.get("href", "")
        and str(uid) in parse_qs(urlparse(node.attrs["href"]).query).get("id", []) for node in descendants(heading))]
    if len(matching) == 1:
        heading = matching[0]
    elif len(headings) == 1:
        heading = headings[0]
    else:
        raise DataError("无法唯一定位目标用户资料标题")
    for node in descendants(heading):
        if node.tag == "a" and "userdetails.php" in node.attrs.get("href", ""):
            ids = parse_qs(urlparse(node.attrs["href"]).query).get("id", [])
            if ids and str(uid) not in ids:
                raise DataError("页面资料 UID 与监控任务不一致")
    samples, used = [], set()
    for node in document.nodes:
        if node.position <= heading.position or node.tag not in {"td", "th", "b", "strong", "span"}:
            continue
        node_text = re.sub(r"\s+", " ", text(node)).strip()
        if normalized(node_text) not in UPLOAD_LABELS:
            continue
        cell = node
        while cell.parent and cell.tag not in {"td", "th"}:
            cell = cell.parent
        if cell.tag not in {"td", "th"}:
            continue
        full = re.sub(r"\s+", " ", text(cell)).strip()
        if normalized(full) in UPLOAD_LABELS:
            siblings = [child for child in cell.parent.children if isinstance(child, Node) and child.tag in {"td", "th"}]
            index = siblings.index(cell)
            if index + 1 >= len(siblings):
                continue
            cell = siblings[index + 1]
            value = text(cell).strip()
        else:
            value = full[len(node_text):].lstrip(" :：\u00a0")
        if cell.position in used:
            continue
        used.add(cell.position)
        try:
            samples.append({"uploaded_bytes": parse_displayed_size(value)})
        except DataError:
            continue
    return dict(_unique_nexus_sample(samples), username=label(text(heading)) or str(uid))


def progress(sample, initial, total, goal=None):
    """直接按读取的上传量计算，达到配置目标即完成，不扣除任何余量。"""
    goal = initial + total if goal is None else goal
    uploaded = sample["uploaded_bytes"]
    complete = uploaded >= goal
    done = total if complete else min(total, max(0, uploaded - initial))
    percent = (Decimal(done) * 100 / total).quantize(Decimal("0.1"))
    if not complete:
        percent = min(Decimal("99.9"), percent)
    return {"status": "completed" if complete else "pending", "percent": float(percent),
            "task_uploaded_bytes": done,
            "remaining_bytes": max(0, goal - uploaded)}


def notification_texts(result, limit=2800):
    """每轮完整汇总三个分组，按段落分批，不截断后续任务。"""
    rows = result["rows"]
    counts = {status: sum(row["status"] == status for row in rows) for status in ("pending", "completed", "error")}
    header = f"上传量进度监控 · {result['updated_at']}\n待完成 {counts['pending']} | 已完成 {counts['completed']} | 查询失败 {counts['error']}"
    blocks = []
    for status, title in (("pending", "待完成"), ("completed", "已完成"), ("error", "查询失败")):
        for row in rows:
            if row["status"] != status:
                continue
            name = row.get("label") or row.get("username") or f"UID {row['uid']}"
            block = f"【{title}】{name}（{row['site_name']}，UID {row['uid']}）\n"
            if status == "error":
                block += f"查询失败：{row['error']}\n目标上传量：{format_bytes(row['target_bytes'])}"
                if row.get("last_uploaded_bytes") is not None:
                    block += f"\n上次成功：{format_bytes(row['last_uploaded_bytes'])}（{row['last_success_at']}）"
            else:
                block += f"当前上传量：{format_bytes(row['uploaded_bytes'])}\n目标上传量：{format_bytes(row['target_bytes'])}"
                block += (f"\n任务总量：{format_bytes(row['total_bytes'])} · 初始：{format_bytes(row['initial_bytes'])}"
                          f"\n本次已完成：{format_bytes(row['task_uploaded_bytes'])}")
                if status == "pending":
                    block += f"\n进度：{row['percent']}% · 剩余：{format_bytes(row['remaining_bytes'])}"
                if row.get("delta_bytes") is not None:
                    block += f"\n较上次：{'+' if row['delta_bytes'] >= 0 else ''}{format_bytes(row['delta_bytes'])}"
                if row.get("warning"):
                    block += "\n" + row["warning"]
            blocks.append(block)
    pages, current = [], header
    for block in blocks:
        if len(current) + len(block) + 2 > limit and current != header:
            pages.append(current)
            current = header
        current += "\n\n" + block
    pages.append(current)
    return [f"{page}\n\n第 {index}/{len(pages)} 批" if len(pages) > 1 else page for index, page in enumerate(pages, 1)]
