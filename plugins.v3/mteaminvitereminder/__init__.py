"""读取直接被邀账号的状态，通过 MoviePilot 通知渠道汇总提醒邀请人。"""
from datetime import datetime, timedelta
from threading import Event, Lock
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin import _PluginBase
from app.db.oper.site import SiteOper
from app.schemas.types import MessageType

from .core import (TZ, DataError, api_roster, assess, delivery_key,
                   manual_roster, positive_id)

DEFAULTS = {
    "enabled": False, "notify": False, "onlyonce": False,
    "cron": "0 9 * * *", "site_id": "", "threshold": 30,
    "api_base": "https://api.m-team.cc/api", "roster_source": "api",
    "invite_path": "/invite/getUserInviteInfo", "manual_uids": "",
    "exclude_uids": "", "request_interval": 2,
}
INVITE_PATHS = ("/invite/getUserInviteInfo", "/invite/getUserInviteHistory",
                "/invite/getUserInviteSendHistory")


class MTeamInviteReminder(_PluginBase):
    """定时检查直接下级；未知资料不猜测，通知通过宿主分发。"""

    plugin_name = "馒头下级登录提醒"
    plugin_desc = "检查直接被邀账号等级和最后浏览时间，通过 MP 已配置渠道汇总提醒。"
    plugin_icon = "https://static.m-team.cc/favicon.ico"
    plugin_version = "0.1.0"
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
        """重复初始化先取消旧任务，再替换配置，防止任务与配置交叉。"""
        self.stop_service()
        with self._lock:
            self._config = dict(DEFAULTS, **(config or {}))
            self._stop = Event()
            self._config_error = None
            try:
                threshold = int(self._config["threshold"])
                interval = float(self._config["request_interval"])
                if not 1 <= threshold < 40 or not 1 <= interval <= 60:
                    raise ValueError("提醒阈值应为 1～39 天，请求间隔为 1～60 秒")
                self._config["threshold"] = threshold
                self._config["request_interval"] = interval
                CronTrigger.from_crontab(self._config["cron"], timezone=TZ)
                self._api_base()
                if self._config["invite_path"] not in INVITE_PATHS:
                    raise ValueError("不支持的邀请查询接口")
                if self._config["roster_source"] not in ("api", "manual"):
                    raise ValueError("名单来源应为 api 或 manual")
            except (ValueError, TypeError) as err:
                self._config_error = str(err)
                logger.error("馒头下级提醒：配置无效")
            once = self._config["onlyonce"]
            if once:
                self._config["onlyonce"] = False
                self.update_config(dict(self._config))
            if once and self.get_state():
                self._scheduler = BackgroundScheduler(timezone=TZ)
                self._scheduler.add_job(self.run_check, "date",
                                        run_date=datetime.now(TZ) + timedelta(seconds=3))
                self._scheduler.start()

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
        self._stop.set()
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def _api_base(self):
        """Key 只发往固定的馒头 HTTPS API 主机，不跟随重定向。"""
        raw = str(self._config["api_base"]).rstrip("/")
        parsed = urlparse(raw)
        if (parsed.scheme != "https" or parsed.hostname not in
                ("api.m-team.cc", "api.m-team.io") or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.path != "/api"
                or parsed.query or parsed.fragment):
            raise ValueError("API 地址仅支持 https://api.m-team.cc/api 或 https://api.m-team.io/api")
        return raw

    def _site(self):
        """只通过宿主 Oper 读取站点；多份馒头配置必须显式选择。"""
        sites = []
        for site in SiteOper().list_order_by_pri():
            host = urlparse(str(site.url or "")).hostname or ""
            if host in ("m-team.cc", "m-team.io") or host.endswith((".m-team.cc", ".m-team.io")):
                sites.append(site)
        selected = str(self._config["site_id"] or "").strip()
        if selected:
            sites = [site for site in sites if str(site.id) == selected]
        if len(sites) != 1:
            raise DataError("请在配置中选择唯一的馒头站点 ID")
        if not sites[0].apikey:
            raise DataError("馒头站点未配置 API Key")
        return sites[0]

    def _request(self, site, path, uid=None):
        """使用官方文档的 query uid；不记录 Key、原始响应或站点错误原文。"""
        response = RequestUtils(
            headers={"x-api-key": site.apikey, "Accept": "application/json",
                     "User-Agent": site.ua or "MoviePilot/MTeamInviteReminder"},
            proxies=settings.PROXY if site.proxy else None, timeout=30, verify=True,
        ).post_res(self._api_base() + path, params={"uid": uid} if uid else {},
                   allow_redirects=False)
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

    def run_check(self):
        """同一实例不并行扫描；发生异常也保存失败状态。"""
        if not self.get_state() or not self._lock.acquire(blocking=False):
            return
        result = {"checked_at": datetime.now(TZ).isoformat(), "rows": [],
                  "error": None, "notification": "未发送"}
        try:
            if self._stop.is_set():
                return
            self._scan(result)
        except DataError as err:
            result["error"] = str(err)
        except Exception as err:
            result["error"] = f"检查异常（{type(err).__name__}），请联调核实"
        finally:
            try:
                self.save_data("last_result", result)
                history = self.get_data("history") or []
                self.save_data("history", ([result] + history)[:30])
                if result["error"]:
                    logger.warning("馒头下级提醒：本次检查失败，请查看详情页")
                    if self._config["notify"] and not self._stop.is_set():
                        try:
                            self.post_message(mtype=MessageType.SiteMessage,
                                              title="馒头下级检查失败",
                                              text=result["error"])
                        except Exception:
                            logger.warning("馒头下级提醒：失败通知未能提交")
            finally:
                self._lock.release()

    def _scan(self, result):
        """查询本人和名单，逐人检查；只保存所需字段，不保存邮箱/IP。"""
        site = self._site()
        owner = self._request(site, "/member/profile")
        if not isinstance(owner, dict):
            raise DataError("本人资料格式异常")
        owner_id = positive_id(owner.get("id"))
        manual = self._config["roster_source"] == "manual"
        if manual:
            roster = manual_roster(self._config["manual_uids"])
        else:
            if self._stop.wait(self._config["request_interval"]):
                result["error"] = "检查已停止，结果不完整"
                return
            roster = api_roster(self._request(site, self._config["invite_path"], owner_id))
        excluded = {row["id"] for row in manual_roster(self._config["exclude_uids"])} if str(self._config["exclude_uids"]).strip() else set()
        ledger = self.get_data("notified") or {}
        pending = []
        now = datetime.now(TZ)
        for row in roster:
            if self._stop.wait(self._config["request_interval"]):
                result["error"] = "检查已停止，结果不完整"
                return
            uid = row["id"]
            view = {"uid": uid, "username": "", "eligible": False}
            try:
                if uid in excluded:
                    view["reason"] = "已排除"
                else:
                    profile = self._request(site, "/member/profile", uid)
                    if not isinstance(profile, dict):
                        raise DataError("下级资料格式异常")
                    view["username"] = str(profile.get("username") or uid)[:80]
                    view.update(assess(profile, row, owner_id, now,
                                       self._config["threshold"], manual))
                    if view["eligible"]:
                        key = delivery_key(owner_id, uid, view)
                        view["notification"] = "本次未登录期间已提交通知" if key in ledger else "待通知"
                        if key not in ledger:
                            pending.append((key, view))
            except DataError as err:
                view["reason"] = "无法判断：" + str(err)
            result["rows"].append(view)
        if not self._config["notify"]:
            result["notification"] = "预览模式：未推送"
            return
        # API 拒绝、隐私设置等导致的未知状态也要告知邀请人，不能静默漏检。
        unknown = [row for row in result["rows"] if row.get("reason", "").startswith("无法判断")]
        if not pending and not unknown:
            result["notification"] = "无新增待提醒账号"
            return
        summary = f"已检查 {len(result['rows'])} 人，新增提醒 {len(pending)} 人，无法判断 {len(unknown)} 人。"
        # 每批最多 15 人，只有确实提交的批次才标记，避免截断正文后漏报。
        for offset in range(0, max(len(pending), 1), 15):
            if self._stop.is_set():
                result["error"] = "检查已停止，通知可能未全部提交"
                return
            batch = pending[offset:offset + 15]
            lines = [summary]
            for _, row in batch:
                lines.append(f"{row['username']}（UID {row['uid']}）：未浏览约 {row['days']} 天；适用 {row['limit']} 天期限。")
            if offset == 0 and unknown:
                lines.append("无法判断 UID：" + ", ".join(str(row["uid"]) for row in unknown[:30]) + "；原因见插件详情。")
            lines.append("请提醒下级本人用浏览器访问站点；做种和 API 查询不算登录。完整结果见插件详情。")
            self.post_message(mtype=MessageType.SiteMessage, title="馒头下级登录检查", text="\n".join(lines))
            for key, row in batch:
                ledger[key] = now.isoformat()
                row["notification"] = "已提交 MP 通知渠道"
            self.save_data("notified", ledger)
        result["notification"] = "已提交 MP 通知渠道（不代表收件端送达）"

    def get_form(self):
        """原生 Vuetify 配置页，无额外前端构建和邮箱配置。"""
        fields = []
        for model, label in (("enabled", "启用插件"), ("notify", "通过 MP 渠道推送给我"), ("onlyonce", "保存后立即检查一次")):
            fields.append({"component": "VSwitch", "props": {"model": model, "label": label}})
        for model, label, kind in (
            ("cron", "检查周期（北京时间）", "VCronField"),
            ("site_id", "馒头站点 ID（仅配置一份时可留空）", "VTextField"),
            ("threshold", "超过多少天未浏览时提醒", "VTextField"),
            ("api_base", "馒头 API 地址", "VTextField"),
            ("request_interval", "请求间隔（秒，至少 1 秒）", "VTextField"),
            ("manual_uids", "手工名单 UID（逗号或换行分隔，需 parentId 验证）", "VTextarea"),
            ("exclude_uids", "排除 UID（逗号或换行分隔）", "VTextarea"),
        ):
            fields.append({"component": kind, "props": {"model": model, "label": label}})
        fields += [
            {"component": "VSelect", "props": {"model": "roster_source", "label": "名单来源", "items": ["api", "manual"]}},
            {"component": "VSelect", "props": {"model": "invite_path", "label": "邀请查询接口（需联调确认）", "items": list(INVITE_PATHS)}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "开发预览版：只检查直接下级。默认关闭推送；API 权限、名单结构和真实宿主加载尚待联调。无需填写 Key，自动读取站点配置。"}},
        ]
        return [{"component": "VForm", "content": fields}], dict(DEFAULTS)

    def get_page(self):
        """通过 text 属性展示内容，避免站点用户名被作为 HTML 渲染。"""
        result = self.get_data("last_result") or {}
        message = self._config_error or result.get("error") or result.get("notification") or "尚未检查"
        page = [{"component": "VAlert", "props": {"type": "warning" if self._config_error or result.get("error") else "info", "text": message}}]
        for row in result.get("rows", []):
            text = f"UID {row['uid']} {row.get('username', '')}：{row.get('reason', '')}"
            if row.get("days") is not None:
                text += f"；未浏览约 {row['days']} 天"
            if row.get("notification"):
                text += "；" + row["notification"]
            page.append({"component": "VAlert", "props": {"type": "warning" if row.get("eligible") else "info", "variant": "tonal", "text": text}})
        return page
