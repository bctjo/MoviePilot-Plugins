"""复用宿主 Vuetify JSON 表单，无独立前端依赖或额外 API。"""
from .core import MAX_TASKS, TASK_UNITS, format_bytes


def node(component, text=None, props=None, content=None):
    result = {"component": component}
    if text is not None:
        result["text"] = str(text)
    if props:
        result["props"] = props
    if content is not None:
        result["content"] = content
    return result


def col(component, width=6, small=12):
    return node("VCol", props={"cols": small, "md": width}, content=[component])


def config_form(sites, error=None):
    """所有任务单独绑定站点；增删事件只改变当前表单模型。"""
    switches = [col(node("VSwitch", props={"model": key, "label": title, "color": "primary", "hide-details": True}), 4)
                for key, title in (("enabled", "启用插件"), ("notify", "每轮发送通知"), ("onlyonce", "立即检查一次"))]
    panels = []
    for index in range(1, MAX_TASKS + 1):
        prefix = f"task_{index}_"
        fields = [
            col(node("VTextField", props={"model": prefix + "label", "label": "客户备注", "placeholder": "可选", "maxlength": 80}), 4),
            col(node("VTextField", props={"model": prefix + "uid", "label": "UID", "required": True, "inputmode": "numeric"}), 4),
            col(node("VSelect", props={"model": prefix + "site_id", "label": "绑定站点", "required": True, "items": sites, "clearable": True}), 4),
            col(node("VTextField", props={"model": prefix + "initial", "label": "初始上传量", "required": True, "type": "number", "min": 0, "step": "any"}), 4, 8),
            col(node("VSelect", props={"model": prefix + "initial_unit", "label": "单位", "items": list(TASK_UNITS)}), 2, 4),
            col(node("VTextField", props={"model": prefix + "total", "label": "任务总量", "required": True, "type": "number", "min": 0, "step": "any"}), 4, 8),
            col(node("VSelect", props={"model": prefix + "total_unit", "label": "单位", "items": list(TASK_UNITS)}), 2, 4),
            col(node("VSwitch", props={"model": prefix + "enabled", "label": "监控此任务", "color": "primary", "hide-details": True}), 6),
            col(node("VBtn", "移除此任务", {"color": "error", "variant": "text", "onClick":
                f"() => {{ model.{prefix}visible = false; model.{prefix}uid = ''; model.{prefix}site_id = ''; model.{prefix}initial = ''; model.{prefix}total = ''; model.{prefix}label = ''; model.{prefix}initial_unit = 'TB'; model.{prefix}total_unit = 'TB'; model.{prefix}enabled = true; }}"}), 6),
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
             "hint": "默认每小时整点", "persistent-hint": True}), 12)]),
        node("div", "目标上传量 = 初始上传量 + 任务总量", {"class": "text-caption text-medium-emphasis my-3"}),
        node("VExpansionPanels", props={"model": "open_panels", "multiple": True, "variant": "accordion", "class": "mb-4"}, content=panels),
        node("VBtn", "添加监控任务", {"color": "primary", "variant": "tonal", "onClick": add_task,
             "disabled": "{{ Array.from({length: " + str(MAX_TASKS) + "}, (_, i) => model['task_' + (i + 1) + '_visible']).every(Boolean) }}"}),
        node("div", f"最多 {MAX_TASKS} 项，保存后生效。",
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
        node("div", "上传任务进度", {"class": "text-subtitle-1 font-weight-bold"}),
        node("VChip", status, {"color": color, "variant": "tonal", "size": "small"}),
    ]), node("VRow", props={"class": "mb-3"}, content=summary)]
    if result.get("status") == "running":
        details.extend([node("VProgressLinear", props={"color": "primary", "height": 6, "rounded": True, "class": "mb-3",
                        "model-value": len(rows) * 100 / result["total"] if result.get("total") else 0}),
                        node("div", result.get("stage", "正在检查"), {"class": "text-body-2 mb-3"})])
    checked = f" · 已检查 {len(rows)}/{result.get('total', 0)} 项" if result.get("status") == "running" else ""
    details.append(node("div", f"最近检查：{result.get('updated_at', '—')}{checked}",
                        {"class": "text-caption text-medium-emphasis pt-3 border-t"}))
    content = [node("VCard", props={"variant": "outlined", "rounded": "lg", "class": "mb-4"},
                    content=[node("VCardText", props={"class": "pa-4 pa-sm-5"}, content=details)])]
    if error or result.get("error"):
        content.append(node("VAlert", props={"type": "warning", "variant": "tonal", "class": "mb-4", "text": error or result["error"]}))
    elif not result:
        content.append(node("VAlert", props={"type": "info", "variant": "tonal", "class": "mb-4",
                                            "text": "在配置页添加用户、绑定站点并填写初始上传量和任务总量；保存后可立即检查。"}))
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
                current = [node("div", format_bytes(row["uploaded_bytes"]))]
                progress_cell = [node("div", f"{row['percent']}%", {"class": "text-body-2 mb-1"}),
                                 node("VProgressLinear", props={"model-value": row["percent"], "height": 5, "rounded": True, "color": section_color}),
                                 node("div", f"剩余 {format_bytes(row['remaining_bytes'])}" if key == "pending" else f"发现达标：{row.get('completed_at') or '—'}",
                                      {"class": "text-caption text-medium-emphasis mt-1"})]
                if row.get("warning"):
                    current.append(node("div", row["warning"], {"class": "text-caption text-warning"}))
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
    return [node("div", props={"class": "pa-3"}, content=content)]
