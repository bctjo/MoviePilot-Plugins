"""检查直接下级及手动添加的账号，通过 MoviePilot 渠道汇总提醒。"""
from datetime import datetime, timedelta
from threading import Event, Lock
from urllib.parse import urlparse
from time import monotonic
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin import _PluginBase
from app.db.oper.site import SiteOper
from app.schemas.types import MessageType

from .core import TZ, DataError, api_roster, assess, manual_roster, parse_time, positive_id

DEFAULTS = {
    "enabled": False, "notify": False, "onlyonce": False,
    "cron": "0 9 * * *", "site_id": "", "threshold": 30, "manual_uids": "",
}
API_BASE = "https://api.m-team.cc/api"
INVITE_PATH = "/invite/getUserInviteHistory"
REQUEST_INTERVAL = 2


class MTeamInviteReminder(_PluginBase):
    """定时检查直接下级和手动添加的账号，通过宿主汇总提醒。"""

    plugin_name = "馒头下级登录提醒"
    plugin_desc = "检查直接下级及手动添加账号的等级和最后浏览时间，通过 MP 渠道汇总提醒。"
    plugin_icon = "https://static.m-team.cc/favicon.ico"
    plugin_version = "0.1.5"
    plugin_author = "bctjo"
    author_url = "https://github.com/bctjo"
    plugin_config_prefix = "mteaminvitereminder_"
    plugin_order = 99
    auth_level = 1

    def __init__(self):
        """仅建立实例状态，导入和构造期间不进行网络访问。"""
        super().__init__()
        self._lock = Lock()
        self._stop = Event()
        self._scheduler = None
        self._config = dict(DEFAULTS)
        self._config_error = None

    def init_plugin(self, config=None):
        """保存启用配置不会取消当前扫描；下一轮使用新配置。"""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        supplied = config or {}
        new_config = {key: supplied.get(key, value) for key, value in DEFAULTS.items()}
        error = None
        try:
            new_config["threshold"] = int(new_config["threshold"])
            if not 1 <= new_config["threshold"] < 40:
                raise ValueError("提醒阈值应为 1～39 天")
            CronTrigger.from_crontab(new_config["cron"], timezone=TZ)
            manual_roster(new_config["manual_uids"])
        except DataError:
            error = "配置无效：额外提醒账号 UID 应为正整数，多个 UID 用英文逗号分隔"
        except (ValueError, TypeError):
            error = "配置无效：请检查周期表达式及 1～39 天的提醒阈值"
        self._config = new_config
        self._config_error = error
        if not self.get_state():
            self._stop.set()
        elif self._stop.is_set():
            self._stop = Event()
        once = bool(new_config["onlyonce"])
        if once:
            self._config["onlyonce"] = False
            self.update_config(dict(self._config))
        logger.info(f"馒头下级提醒：配置已保存，启用={self.get_state()}，MP 推送={bool(new_config['notify'])}；正在执行的扫描使用启动时配置")
        if error:
            logger.warning("馒头下级提醒：" + error)
        if once and self.get_state():
            self._scheduler = BackgroundScheduler(timezone=TZ)
            self._scheduler.add_job(self.run_check, "date",
                                    run_date=datetime.now(TZ) + timedelta(seconds=3))
            self._scheduler.start()
            logger.info("馒头下级提醒：已安排立即检查，3 秒后启动")

    def get_state(self):
        """无效配置不注册检查任务。"""
        return bool(self._config["enabled"] and not self._config_error)

    @staticmethod
    def get_command():
        """暂不注册远程命令。"""
        return []

    def get_api(self):
        """不暴露可读取凭据或直接触发网络的额外 API。"""
        return []

    def get_service(self):
        """由宿主管理每日检查任务。"""
        if not self.get_state():
            return []
        return [{"id": "MTeamInviteReminder", "name": self.plugin_name,
                 "trigger": CronTrigger.from_crontab(self._config["cron"], timezone=TZ),
                 "func": self.run_check, "kwargs": {}}]

    def stop_service(self):
        """通知正在执行的检查退出，并清理立即检查的调度器。"""
        logger.info("馒头下级提醒：收到停止服务请求，当前扫描将在请求结束后退出")
        self._stop.set()
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def _api_base(self):
        """固定官方 API 地址，旧版内部配置不再影响请求。"""
        return API_BASE

    def _site(self, config=None):
        """只通过宿主 Oper 读取站点；多份馒头配置必须显式选择。"""
        sites = []
        for site in SiteOper().list_order_by_pri():
            host = urlparse(str(site.url or "")).hostname or ""
            if host in ("m-team.cc", "m-team.io") or host.endswith((".m-team.cc", ".m-team.io")):
                sites.append(site)
        selected = str((config or self._config)["site_id"] or "").strip()
        if selected:
            sites = [site for site in sites if str(site.id) == selected]
        if len(sites) != 1:
            raise DataError("请在配置中选择唯一的馒头站点 ID")
        if not sites[0].apikey:
            raise DataError("馒头站点未配置 API Key")
        return sites[0]

    def _request(self, site, path, uid=None):
        """使用 query uid 查询资料；不记录 Key、原始响应或站点错误原文。"""
        started = monotonic()
        label = f"{path} UID {uid}" if uid else path
        logger.info(f"馒头下级提醒：开始请求 {label}（超时 30 秒）")
        try:
            response = RequestUtils(
                headers={"x-api-key": site.apikey, "Accept": "application/json",
                         "User-Agent": site.ua or "MoviePilot/MTeamInviteReminder"},
                proxies=settings.PROXY if site.proxy else None, timeout=30, verify=True,
            ).post_res(self._api_base() + path, params={"uid": uid} if uid else {},
                       allow_redirects=False)
        except Exception as err:
            logger.warning(f"馒头下级提醒：请求异常 {label}，{type(err).__name__}，耗时 {monotonic() - started:.1f} 秒")
            raise DataError(f"API 请求异常（{type(err).__name__}）") from None
        logger.info(f"馒头下级提醒：请求结束 {label}，HTTP {response.status_code if response is not None else '无响应'}，耗时 {monotonic() - started:.1f} 秒")
        if response is None:
            raise DataError("API 请求失败或超时")
        if response.status_code != 200:
            raise DataError(f"API 返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise DataError("API 响应不是 JSON") from None
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise DataError("API 拒绝请求，请检查 Key 和接口权限")
        return payload.get("data")

    def _progress(self, result, stage):
        """每个步骤持久化进度，页面关闭或刷新不影响后台任务。"""
        result["stage"] = stage
        result["updated_at"] = datetime.now(TZ).isoformat()
        logger.info(f"馒头下级提醒 [{result.get('run_id', '-')}]：{stage}")
        self.save_data("last_result", result)

    def run_check(self):
        """扫描使用配置和停止信号的快照，避免重配串扰。"""
        if not self.get_state():
            logger.info("馒头下级提醒：插件未启用或配置无效，跳过检查")
            return
        if not self._lock.acquire(blocking=False):
            logger.info("馒头下级提醒：已有检查正在运行，本次触发跳过")
            return
        config, stop = dict(self._config), self._stop
        started = monotonic()
        result = {"run_id": uuid4().hex[:8], "checked_at": datetime.now(TZ).isoformat(),
                  "rows": [], "total": None, "status": "running", "error": None,
                  "notification": "未发送"}
        try:
            self._progress(result, "开始检查")
            if stop.is_set():
                result["error"] = "检查已停止，结果不完整"
            else:
                self._scan(result, config, stop)
        except DataError as err:
            result["error"] = str(err)
        except Exception as err:
            # 不输出异常原文，避免 HTTP 库异常携带请求头或凭据。
            result["error"] = f"检查异常（{type(err).__name__}），请查看最后一步日志"
        finally:
            try:
                result["status"] = "stopped" if stop.is_set() else "failed" if result["error"] else "completed"
                result["elapsed_seconds"] = round(monotonic() - started, 1)
                eligible = sum(bool(row.get("eligible")) for row in result["rows"])
                self._progress(result, f"检查结束：{result['status']}，已检查 {len(result['rows'])}/{result.get('total') if result.get('total') is not None else '?'} 人，符合提醒 {eligible} 人，耗时 {result['elapsed_seconds']} 秒" + (f"；{result['error']}" if result['error'] else ""))
                history = self.get_data("history") or []
                self.save_data("history", ([result] + history)[:30])
                if result["error"]:
                    logger.warning("馒头下级提醒：" + result["error"])
                    if config["notify"] and not stop.is_set():
                        try:
                            self.post_message(mtype=MessageType.SiteMessage,
                                              title="馒头下级检查失败", text=result["error"])
                        except Exception as err:
                            logger.warning(f"馒头下级提醒：失败通知提交异常（{type(err).__name__}）")
            finally:
                self._lock.release()

    def _scan(self, result, config=None, stop=None):
        """查询本人和名单，逐人检查；只保存所需字段，不保存邮箱/IP。"""
        config = config or dict(self._config)
        stop = stop or self._stop
        site = self._site(config)
        self._progress(result, "正在读取本人资料")
        owner = self._request(site, "/member/profile")
        if not isinstance(owner, dict):
            raise DataError("本人资料格式异常")
        owner_id = positive_id(owner.get("id"))
        if stop.wait(REQUEST_INTERVAL):
            result["error"] = "检查已停止，结果不完整"
            return
        self._progress(result, "正在读取直接下级名单")
        roster = [dict(row, manual=False) for row in api_roster(self._request(site, INVITE_PATH, owner_id))]
        direct_count = len(roster)
        seen = {row["id"] for row in roster}
        for row in manual_roster(config["manual_uids"]):
            if row["id"] not in seen:
                roster.append(dict(row, manual=True))
                seen.add(row["id"])
        result["total"] = len(roster)
        self._progress(result, f"取得 {direct_count} 个直接下级，追加 {len(roster) - direct_count} 个手动账号，共 {len(roster)} 人，开始逐人检查")
        pending = []
        now = datetime.now(TZ)
        for index, row in enumerate(roster, 1):
            if stop.wait(REQUEST_INTERVAL):
                result["error"] = "检查已停止，结果不完整"
                return
            uid = row["id"]
            view = {"uid": uid, "username": "", "eligible": False,
                    "source": "手动添加" if row["manual"] else "直接下级"}
            try:
                self._progress(result, f"正在检查 {index}/{len(roster)}：UID {uid}")
                profile = self._request(site, "/member/profile", uid)
                if not isinstance(profile, dict):
                    raise DataError("账号资料格式异常")
                view["username"] = str(profile.get("username") or uid)[:80]
                view.update(assess(profile, row, owner_id, now, config["threshold"], manual=row["manual"]))
                if view["eligible"]:
                    view["notification"] = "待通知" if config["notify"] else "未开启"
                    pending.append(view)
            except DataError as err:
                view["reason"] = "无法判断：" + str(err)
            except Exception as err:
                view["reason"] = f"无法判断：查询异常（{type(err).__name__}）"
            result["rows"].append(view)
            self._progress(result, f"已检查 {index}/{len(roster)}：UID {uid}，{view['reason']}")
        if stop.is_set():
            result["error"] = "检查已停止，结果不完整"
            return
        if not config["notify"]:
            result["notification"] = "预览模式：未推送"
            return
        # API 拒绝、隐私设置等导致的未知状态也要告知邀请人，不能静默漏检。
        unknown = [row for row in result["rows"] if row.get("reason", "").startswith("无法判断")]
        if not pending and not unknown:
            result["notification"] = "本次无待提醒账号"
            return
        summary = f"已检查 {len(result['rows'])} 人，符合提醒 {len(pending)} 人，无法判断 {len(unknown)} 人。"
        # 每批最多 15 人，只有确实提交的批次才标记，避免截断正文后漏报。
        for offset in range(0, max(len(pending), 1), 15):
            if stop.is_set():
                result["error"] = "检查已停止，通知可能未全部提交"
                return
            batch = pending[offset:offset + 15]
            lines = [summary]
            for row in batch:
                lines.append(f"{row['username']}（UID {row['uid']}）：未浏览约 {row['days']} 天；适用 {row['limit']} 天期限。")
            if offset == 0 and unknown:
                lines.append("无法判断 UID：" + ", ".join(str(row["uid"]) for row in unknown[:30]) + "；原因见插件详情。")
            lines.append("请提醒账号本人用浏览器访问站点；做种和 API 查询不算登录。完整结果见插件详情。")
            self._progress(result, f"正在提交 MP 通知，第 {offset // 15 + 1} 批，{len(batch)} 个账号")
            self.post_message(mtype=MessageType.SiteMessage, title="馒头下级登录检查", text="\n".join(lines))
            for row in batch:
                row["notification"] = "已提交 MP 通知渠道"
        result["notification"] = "已提交 MP 通知渠道（不代表收件端送达）"

    def get_form(self):
        """按官方插件的 VRow/VCol 结构排版，仅展示日常设置。"""
        def col(component, width=6):
            return {"component": "VCol", "props": {"cols": 12, "md": width}, "content": [component]}

        switches = [col({"component": "VSwitch", "props": {"model": model, "label": label, "hide-details": True}}, 4)
                    for model, label in (("enabled", "启用插件"), ("onlyonce", "保存后立即检查一次"),
                                         ("notify", "通过 MP 渠道推送给我"))]
        settings_row = [
            col({"component": "VCronField", "props": {"model": "cron", "label": "检查周期（北京时间）", "hint": "默认每天 09:00 检查", "persistent-hint": True}}, 8),
            col({"component": "VTextField", "props": {"model": "threshold", "label": "未浏览提醒阈值（天）", "type": "number", "min": 1, "max": 39, "hint": "超过完整 30 天时提醒", "persistent-hint": True}}, 4),
            col({"component": "VTextField", "props": {"model": "manual_uids", "label": "额外提醒账号 UID", "placeholder": "例如：123456,234567", "hint": "多个 UID 用英文逗号分隔，追加到下级名单后；可添加非下级账号，自动去重。", "persistent-hint": True, "clearable": True}}, 12),
        ]
        sites = []
        for site in SiteOper().list_order_by_pri():
            host = urlparse(str(site.url or "")).hostname or ""
            if host in ("m-team.cc", "m-team.io") or host.endswith((".m-team.cc", ".m-team.io")):
                sites.append({"title": str(site.name), "value": str(site.id)})
        if len(sites) > 1:
            settings_row.append(col({"component": "VSelect", "props": {"model": "site_id", "label": "馒头站点", "items": sites}}, 12))
        return [{"component": "VForm", "props": {"class": "pa-2"}, "content": [
            {"component": "VRow", "props": {"class": "mb-2"}, "content": switches},
            {"component": "VRow", "props": {"class": "mb-2"}, "content": settings_row},
            {"component": "VRow", "content": [col({"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "自动读取馒头站点和 API Key，检查直接下级及手动添加的账号。每次检查均通过 MP 渠道汇总符合条件或无法判断的账号；关闭推送时仅预览。"}}, 12)]},
        ]}], dict(DEFAULTS)

    @staticmethod
    def _display_time(value):
        """页面统一显示北京时间到秒，不暴露原始 ISO 时间串。"""
        try:
            return parse_time(value).strftime("%Y-%m-%d %H:%M:%S")
        except DataError:
            return "—"

    def get_page(self):
        """汇总卡片和可横向滚动的表格，文本渲染账号资料。"""
        result = self.get_data("last_result") or {}
        rows = result.get("rows", [])
        running = result.get("status") == "running"
        status, status_color = {
            "running": ("检查中", "primary"),
            "completed": ("已完成", "success"),
            "failed": ("检查失败", "error"),
            "stopped": ("已停止", "warning"),
        }.get(result.get("status"), ("尚未检查", "secondary"))
        total = result.get("total")
        eligible = sum(bool(row.get("eligible")) for row in rows)
        unknown = sum(row.get("reason", "").startswith("无法判断") for row in rows)
        elapsed = result.get("elapsed_seconds")
        summary = []
        for title, value, color in (
            ("已检查 / 总人数", f"{len(rows)} / {total if total is not None else '—'}" if result else "—", "text-primary"),
            ("符合提醒", str(eligible) if result else "—", "text-warning" if eligible else ""),
            ("无法判断", str(unknown) if result else "—", "text-warning" if unknown else ""),
            ("本次耗时", f"{elapsed:g} 秒" if elapsed is not None else "—", ""),
        ):
            summary.append({"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis mb-1"}, "text": title},
                {"component": "div", "props": {"class": f"text-h5 font-weight-bold {color}".strip()}, "text": value},
            ]})
        details = [
            {"component": "div", "props": {"class": "d-flex flex-wrap align-center justify-space-between ga-2 mb-4"}, "content": [
                {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold"}, "text": "最近一次检查"},
                {"component": "VChip", "props": {"color": status_color, "variant": "tonal", "size": "small"}, "text": status},
            ]},
            {"component": "VRow", "props": {"class": "mb-3"}, "content": summary},
        ]
        if running:
            details.extend([
                {"component": "VProgressLinear", "props": {"model-value": len(rows) * 100 / total if total else 0, "indeterminate": total is None, "color": "primary", "rounded": True, "class": "mb-3", "height": 6}},
                {"component": "div", "props": {"class": "text-body-2 mb-3"}, "text": result.get("stage") or "正在准备检查…"},
            ])
        elif not result:
            details.append({"component": "div", "props": {"class": "text-body-2 text-medium-emphasis"}, "text": "保存配置并立即检查一次后，这里会显示结果。"})
        if result:
            footer = [{"component": "span", "text": f"最近更新：{self._display_time(result.get('updated_at') or result.get('checked_at'))}（北京时间）"}]
            if running:
                footer.append({"component": "span", "text": "刷新详情可查看最新进度"})
            details.append({"component": "div", "props": {"class": "d-flex flex-wrap align-center justify-space-between ga-2 text-caption text-medium-emphasis pt-3 border-t"}, "content": footer})
        message = self._config_error or result.get("error") or result.get("notification")
        table_rows = []
        for row in rows:
            cells = [
                {"component": "td", "props": {"class": "py-3"}, "content": [
                    {"component": "div", "text": str(row.get("username") or "—")},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": f"UID {row['uid']} · {row.get('source') or '直接下级'}"},
                ]},
                {"component": "td", "text": f"{row['days']} 天" if row.get("days") is not None else "—"},
                {"component": "td", "props": {"class": "text-warning" if row.get("eligible") else ""}, "text": row.get("reason", "—")},
                {"component": "td", "text": row.get("notification", "—")},
                {"component": "td", "text": self._display_time(row.get("last_browse"))},
            ]
            table_rows.append({"component": "tr", "content": cells})
        content = [
            {"component": "VCard", "props": {"variant": "outlined", "rounded": "lg", "class": "mb-4"}, "content": [{"component": "VCardText", "props": {"class": "pa-4 pa-sm-5"}, "content": details}]},
        ]
        if message:
            content.append({"component": "VAlert", "props": {"type": "warning" if self._config_error or result.get("error") else "info", "variant": "tonal", "class": "mb-4", "text": message}})
        if table_rows:
            content.append({"component": "div", "props": {"class": "overflow-x-auto"}, "content": [
                {"component": "VTable", "props": {"hover": True, "style": "min-width: 720px"}, "content": [
                    {"component": "thead", "content": [{"component": "tr", "content": [{"component": "th", "props": {"class": "text-start"}, "text": title} for title in ("检查账号", "未浏览", "判断结果", "MP 通知", "最后浏览时间")]}]},
                    {"component": "tbody", "content": table_rows},
                ]},
            ]})
        return [{"component": "div", "props": {"class": "pa-3"}, "content": content}]
