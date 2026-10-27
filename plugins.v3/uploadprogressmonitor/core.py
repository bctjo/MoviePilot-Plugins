"""上传量任务、字节换算、站点资料解析及通知排版，不访问网络。"""
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, ROUND_CEILING, localcontext
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
MAX_TASKS = 100
UNITS = {name: 1024 ** power for power, name in enumerate(("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"))}


class DataError(ValueError):
    """配置或站点资料不足以可靠判断进度。"""


def positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise DataError("UID 和站点 ID 必须为正整数")
    return int(value)


def label(value):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))).strip()[:80]


def target_bytes(value, unit):
    try:
        number = Decimal(str(value))
        if isinstance(value, bool) or not number.is_finite() or number <= 0 or unit not in UNITS:
            raise ValueError
        if number > Decimal(2 ** 64 - 1) / UNITS[unit] or len(number.as_tuple().digits) > 128:
            raise ValueError
        with localcontext() as context:
            context.prec = max(64, len(number.as_tuple().digits) + 20)
            return max(1, int((number * UNITS[unit]).to_integral_value(rounding=ROUND_CEILING)))
    except (DecimalException, ValueError, TypeError, OverflowError):
        raise DataError("目标上传量必须为大于 0 的数值，并选择有效单位") from None


def task_defaults():
    """固定字段供宿主 JSON 表单使用，空白任务不参与扫描。"""
    result = {"open_panels": [1]}
    for index in range(1, MAX_TASKS + 1):
        for key, value in {"visible": index == 1, "enabled": True, "site_id": "", "uid": "",
                           "target": "", "unit": "TiB", "label": ""}.items():
            result[f"task_{index}_{key}"] = value
    return result


def parse_tasks(config):
    tasks, seen = [], set()
    for index in range(1, MAX_TASKS + 1):
        get = lambda key, default=None: config.get(f"task_{index}_{key}", default)
        if not get("visible", index == 1) or not get("enabled", True):
            continue
        uid, site_id, target = get("uid", ""), get("site_id", ""), get("target", "")
        if all(value is None or str(value).strip() == "" for value in (uid, site_id, target)):
            continue
        try:
            uid, site_id = positive_id(uid), positive_id(site_id)
            goal = target_bytes(target, get("unit", "TiB"))
        except DataError as error:
            raise DataError(f"任务 {index}：{error}") from None
        key = f"{site_id}:{uid}"
        if key in seen:
            raise DataError(f"任务 {index}：同一站点和 UID 已存在，请合并重复任务")
        seen.add(key)
        tasks.append({"key": key, "slot": index, "uid": uid, "site_id": site_id,
                      "target_bytes": goal, "label": label(get("label"))})
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
    return {"username": label(data.get("username")) or str(uid), "uploaded_bytes": uploaded,
            "lower_bound_bytes": uploaded, "approximate": False}


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
    center = number * factor
    if power == 0 and number == number.to_integral_value():
        return int(center), int(center), False, Decimal(1)
    decimals = len(raw_number.split(".")[1]) if "." in raw_number else 0
    step = Decimal(factor) / (10 ** decimals)
    lower = int(max(Decimal(0), center - step / 2).to_integral_value(rounding=ROUND_CEILING))
    return int(center), lower, True, step


def cell_sample(cell, value):
    uploaded, lower, approximate, step = parse_displayed_size(value)
    exact = set()
    for node in descendants(cell):
        for name in ("data-bytes", "data-raw", "title"):
            attribute = node.attrs.get(name)
            if not attribute:
                continue
            match = re.fullmatch(r"\s*([0-9]+(?:,[0-9]{3})*)\s*(?:bytes?|字节|位元組)?\s*", attribute, re.I)
            if match:
                candidate = int(match.group(1).replace(",", ""))
                if abs(Decimal(candidate) - uploaded) <= step / 2 + 1:
                    exact.add(candidate)
    if len(exact) > 1:
        raise DataError("上传量精确字节数存在冲突")
    if exact:
        uploaded = lower = exact.pop()
        approximate = False
    return {"uploaded_bytes": uploaded, "lower_bound_bytes": lower, "approximate": approximate}


def parse_nexus_profile(source, uid):
    """解析 NexusPHP 资料页，排除导航栏和真实上传等不同记账指标。"""
    if not isinstance(source, str) or len(source) > 5_000_000:
        raise DataError("用户详情页为空或超过解析大小限制")
    document = Document(source)
    headings = [node for node in document.nodes if node.tag == "h1"]
    if not headings:
        raise DataError("未找到用户资料标题，请检查 Cookie、登录状态或页面布局")
    if any(node.tag == "form" and urlparse(node.attrs.get("action", "")).path.rsplit("/", 1)[-1] in {"login.php", "takelogin.php"}
           for node in document.nodes):
        raise DataError("站点返回登录页，请更新 MP 站点 Cookie")
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
    labels = {"上传", "上传量", "已上传", "上傳", "上傳量", "已上傳", "uploaded", "upload"}
    samples, used = [], set()
    for node in document.nodes:
        if node.position <= heading.position or node.tag not in {"td", "th", "b", "strong", "span"}:
            continue
        node_text = re.sub(r"\s+", " ", text(node)).strip()
        if normalized(node_text) not in labels:
            continue
        cell = node
        while cell.parent and cell.tag not in {"td", "th"}:
            cell = cell.parent
        if cell.tag not in {"td", "th"}:
            continue
        full = re.sub(r"\s+", " ", text(cell)).strip()
        if normalized(full) in labels:
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
            samples.append(cell_sample(cell, value))
        except DataError:
            continue
    if not samples:
        raise DataError("未找到目标用户上传量，可能为隐私限制、Cookie 失效或页面改版")
    if len({(item["uploaded_bytes"], item["lower_bound_bytes"]) for item in samples}) != 1:
        raise DataError("页面包含多个不同的上传量，无法可靠判断")
    return dict(samples[0], username=label(text(heading)) or str(uid))


def progress(sample, goal):
    """格式化页面的下界达到目标后才确认完成，避免四舍五入提前结单。"""
    complete = sample["lower_bound_bytes"] >= goal
    percent = min(Decimal(100), Decimal(sample["lower_bound_bytes"]) * 100 / goal)
    return {"status": "completed" if complete else "pending", "percent": float(percent.quantize(Decimal("0.1"))),
            "remaining_bytes": max(0, goal - sample["lower_bound_bytes"]),
            "precision_note": "页面数值已四舍五入，临界值待确认" if sample["approximate"] and sample["uploaded_bytes"] >= goal and not complete else ""}


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
                prefix = "约 " if row["approximate"] else ""
                block += f"当前上传量：{prefix}{format_bytes(row['uploaded_bytes'])}\n目标上传量：{format_bytes(row['target_bytes'])}"
                if status == "pending":
                    block += f"\n进度：{row['percent']}% · 剩余：{format_bytes(row['remaining_bytes'])}"
                if row.get("delta_bytes") is not None:
                    block += f"\n较上次：{'+' if row['delta_bytes'] >= 0 else ''}{format_bytes(row['delta_bytes'])}"
                for note in (row.get("precision_note"), row.get("warning")):
                    if note:
                        block += "\n" + note
            blocks.append(block)
    pages, current = [], header
    for block in blocks:
        if len(current) + len(block) + 2 > limit and current != header:
            pages.append(current)
            current = header
        current += "\n\n" + block
    pages.append(current)
    return [f"{page}\n\n第 {index}/{len(pages)} 批" if len(pages) > 1 else page for index, page in enumerate(pages, 1)]
