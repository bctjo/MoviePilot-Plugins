"""复用宿主 Vuetify JSON 表单，无独立前端依赖或额外 API。"""
from .core import MAX_TASKS, format_bytes


def node(component, text=None, props=None, content=None):
    result = {"component": component}
    if text is not None:
        result["text"] = str(text)
    if props:
        result["props"] = props
    if content is not None:
        result["content"] = content
    return result


def col(component, width=6):
    return node("VCol", props={"cols": 12, "md": width}, content=[component])


def config_form(sites, error=None):
    """所有任务单独绑定站点；增删事件只改变当前表单模型。"""
    switches = [col(node("VSwitch", props={"model": key, "label": title, "color": "primary", "hide-details": True}), 4)
                for key, title in (("enabled", "启用插件"), ("notify", "每轮推送进度汇总"), ("onlyonce", "保存后立即检查一次"))]
    panels = []
    for index in range(1, MAX_TASKS + 1):
        prefix = f"task_{index}_"
        fields = [
            col(node("VTextField", props={"model": prefix + "label", "label": "客户备注（可选）", "maxlength": 80}), 6),
            col(node("VTextField", props={"model": prefix + "uid", "label": "用户 UID", "inputmode": "numeric", "placeholder": "填写该站点的 UID"}), 6),
            col(node("VSelect", props={"model": prefix + "site_id", "label": "该用户绑定的站点", "items": sites, "clearable": True}), 6),
            col(node("VTextField", props={"model": prefix + "target", "label": "目标累计上传量", "type": "number", "min": 0, "step": "any"}), 3),
            col(node("VSelect", props={"model": prefix + "unit", "label": "目标单位", "items": [
                {"title": "GiB（站点 GB）", "value": "GiB"}, {"title": "TiB（站点 TB）", "value": "TiB"},
                {"title": "PiB（站点 PB）", "value": "PiB"}]}), 3),
            col(node("VSwitch", props={"model": prefix + "enabled", "label": "监控此任务", "color": "primary", "hide-details": True}), 6),
            col(node("VBtn", "移除此任务", {"color": "error", "variant": "text", "onClick":
                f"() => {{ model.{prefix}visible = false; model.{prefix}uid = ''; model.{prefix}site_id = ''; model.{prefix}target = ''; model.{prefix}label = ''; model.{prefix}unit = 'TiB'; model.{prefix}enabled = true; }}"}), 6),
        ]
        title = node("VExpansionPanelTitle", content=[node("div", props={"class": "d-flex align-center ga-3"}, content=[
            node("span", f"任务 {index}"), node("VChip", props={"size": "small", "variant": "tonal", "color": "primary"},
                content=[node("span", props={"textContent": "{{ " + prefix + "label || (" + prefix + "uid ? 'UID ' + " + prefix + "uid : '待配置') }}"})]),
        ])])
        panels.append(node("VExpansionPanel", props={"value": index, "show": "{{ " + prefix + "visible }}"},
                           content=[title, node("VExpansionPanelText", content=[node("VRow", content=fields)])]))
    add_task = "() => { for (let i = 1; i <= " + str(MAX_TASKS) + "; i++) { const key = 'task_' + i + '_'; if (!model[key + 'visible']) { model[key + 'visible'] = true; model.open_panels = [i]; break; } } }"
    content = [
        node("VRow", content=switches),
        node("VRow", content=[col(node("VCronField", props={"model": "cron", "label": "检查周期（北京时间）",
             "hint": "默认每小时整点检查并汇总；0 * * * *", "persistent-hint": True}), 12)]),
        node("VAlert", props={"type": "info", "variant": "tonal", "class": "my-4",
             "text": "目标按站点累计上传量计算，单位按 1024 换算。馒头复用 MP 站点的 API Key；Audiences 及同模板 NexusPHP 站点复用 Cookie。完成任务也继续参与每轮汇总。"}),
        node("VExpansionPanels", props={"model": "open_panels", "multiple": True, "variant": "accordion", "class": "mb-4"}, content=panels),
        node("VBtn", "添加监控任务", {"color": "primary", "variant": "tonal", "onClick": add_task,
             "disabled": "{{ Array.from({length: " + str(MAX_TASKS) + "}, (_, i) => model['task_' + (i + 1) + '_visible']).every(Boolean) }}"}),
        node("div", f"最多 {MAX_TASKS} 项；编辑完成后保存生效。留空的任务不参与检查，暂停任务可关闭“监控此任务”。",
             {"class": "text-caption text-medium-emphasis mt-3"}),
    ]
    if not sites:
        content.insert(0, node("VAlert", props={"type": "warning", "variant": "tonal", "class": "mb-4",
                                              "text": "请先在 MP 站点管理中添加站点，并配置 API Key 或 Cookie。"}))
    if error:
        content.insert(0, node("VAlert", props={"type": "warning", "variant": "tonal", "class": "mb-4", "text": error}))
    return [node("VForm", props={"class": "pa-2"}, content=content)]


def details_page(result, error=None):
    """按待完成、已完成和查询失败分组展示，陈旧结果明确标注。"""
    rows = result.get("rows", [])
    status, color = {"running": ("检查中", "primary"), "completed": ("本轮已检查", "success"),
                     "failed": ("本轮失败", "error"), "stopped": ("已停止", "warning")}.get(result.get("status"), ("尚未检查", "secondary"))
    counts = {key: sum(row["status"] == key for row in rows) for key in ("pending", "completed", "error")}
    summary = []
    for title, count, css in (("待完成", counts["pending"], "text-warning"), ("已完成", counts["completed"], "text-success"),
                               ("查询失败", counts["error"], "text-error"), ("监控任务", result.get("total", 0), "text-primary")):
        summary.append(col(node("div", content=[node("div", title, {"class": "text-caption text-medium-emphasis mb-1"}),
                                                   node("div", count, {"class": "text-h5 font-weight-bold " + css})]), 3))
        summary[-1]["props"]["cols"] = 6
    details = [node("div", props={"class": "d-flex align-center justify-space-between mb-4"}, content=[
        node("div", "累计上传量进度", {"class": "text-subtitle-1 font-weight-bold"}),
        node("VChip", status, {"color": color, "variant": "tonal", "size": "small"}),
    ]), node("VRow", props={"class": "mb-3"}, content=summary)]
    if result.get("status") == "running":
        details.extend([node("VProgressLinear", props={"color": "primary", "height": 6, "rounded": True, "class": "mb-3",
                        "model-value": len(rows) * 100 / result["total"] if result.get("total") else 0}),
                        node("div", result.get("stage", "正在检查"), {"class": "text-body-2 mb-3"})])
    details.append(node("div", f"最近更新：{result.get('updated_at', '—')}（北京时间） · 已检查 {len(rows)}/{result.get('total', 0)} 项",
                        {"class": "text-caption text-medium-emphasis pt-3 border-t"}))
    content = [node("VCard", props={"variant": "outlined", "rounded": "lg", "class": "mb-4"},
                    content=[node("VCardText", props={"class": "pa-4 pa-sm-5"}, content=details)])]
    if error or result.get("error"):
        content.append(node("VAlert", props={"type": "warning", "variant": "tonal", "class": "mb-4", "text": error or result["error"]}))
    elif not result:
        content.append(node("VAlert", props={"type": "info", "variant": "tonal", "class": "mb-4",
                                            "text": "在配置页添加用户、绑定站点并填写目标上传量；保存后可立即检查。"}))
    if result.get("notification"):
        content.append(node("div", result["notification"], {"class": "text-caption text-medium-emphasis mb-4"}))
    for key, title, section_color in (("pending", "待完成", "warning"), ("completed", "已完成", "success"), ("error", "查询失败", "error")):
        matching = [row for row in rows if row["status"] == key]
        if not matching:
            continue
        table_rows = []
        for row in matching:
            account = [node("div", row.get("label") or row.get("username") or "—", {"class": "font-weight-medium"}),
                       node("div", f"UID {row['uid']}" + (f" · {row['username']}" if row.get("label") and row.get("username") else ""),
                            {"class": "text-caption text-medium-emphasis"})]
            if key == "error":
                current = [node("div", row["error"], {"class": "text-error text-body-2"})]
                if row.get("last_uploaded_bytes") is not None:
                    current.append(node("div", f"上次成功：{format_bytes(row['last_uploaded_bytes'])}（{row['last_success_at']}）", {"class": "text-caption text-medium-emphasis"}))
                progress_cell = [node("span", "—")]
            else:
                current = [node("div", ("约 " if row["approximate"] else "") + format_bytes(row["uploaded_bytes"]))]
                progress_cell = [node("div", f"{row['percent']}%", {"class": "text-body-2 mb-1"}),
                                 node("VProgressLinear", props={"model-value": row["percent"], "height": 5, "rounded": True, "color": section_color}),
                                 node("div", f"剩余 {format_bytes(row['remaining_bytes'])}" if key == "pending" else f"首次发现达标：{row.get('completed_at') or '—'}",
                                      {"class": "text-caption text-medium-emphasis mt-1"})]
                for note in (row.get("precision_note"), row.get("warning")):
                    if note:
                        current.append(node("div", note, {"class": "text-caption text-warning"}))
            delta = row.get("delta_bytes")
            cells = [node("td", props={"class": "py-3"}, content=account), node("td", row["site_name"]),
                     node("td", content=current), node("td", format_bytes(row["target_bytes"])),
                     node("td", props={"style": "min-width: 180px"}, content=progress_cell),
                     node("td", ("+" if delta is not None and delta >= 0 else "") + format_bytes(delta)),
                     node("td", row.get("checked_at", "—"))]
            table_rows.append(node("tr", content=cells))
        content.append(node("div", props={"class": "mb-6"}, content=[
            node("div", props={"class": "d-flex align-center ga-2 mb-3"}, content=[
                node("div", title, {"class": "text-subtitle-1 font-weight-bold"}),
                node("VChip", len(matching), {"size": "small", "color": section_color, "variant": "tonal"}),
            ]),
            node("div", props={"class": "overflow-x-auto"}, content=[node("VTable", props={"hover": True, "style": "min-width: 1000px"}, content=[
                node("thead", content=[node("tr", content=[node("th", title, {"class": "text-start"}) for title in ("监控用户", "绑定站点", "当前上传量", "目标上传量", "进度", "较上次变化", "检查时间")])]),
                node("tbody", content=table_rows),
            ])]),
        ]))
    if any(row.get("approximate") for row in rows):
        content.append(node("div", "标注“约”的上传量来自站点格式化显示；页面精度不足时保留待完成，避免提前判定达标。",
                            {"class": "text-caption text-medium-emphasis"}))
    return [node("div", props={"class": "pa-3"}, content=content)]
