"""按用户和站点监控累计上传量，每轮通过 MP 通知渠道汇总进度。"""
from copy import deepcopy
from datetime import datetime
import re
from threading import Event, Lock
from time import monotonic
from urllib.parse import urljoin, urlparse

from apscheduler.triggers.cron import CronTrigger
from app.sdk import scheduler as scheduler_sdk
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin import _PluginBase
from app.db.oper.site import SiteOper
from app.schemas.types import MessageType

from .core import (TZ, DataError, label, notification_texts, parse_mteam_profile,
                   parse_nexus_profile, parse_tasks, progress, task_defaults)
from .ui import config_form, details_page

DEFAULTS = {"enabled": False, "notify": True, "onlyonce": False, "cron": "0 * * * *", **task_defaults()}
REQUEST_INTERVAL = 2
API_BASE = "https://api.m-team.cc/api"


def adapter_for(site):
    """馒头使用官方 API，其余选定站点使用 NexusPHP 资料页模板。"""
    host = (urlparse(str(site.url or "")).hostname or "").lower()
    return "mteam" if host in ("m-team.cc", "m-team.io") or host.endswith((".m-team.cc", ".m-team.io")) else "nexusphp"


class UploadProgressMonitor(_PluginBase):
    """目标为站点累计上传量，不按启动后的新增流量计算。"""
    plugin_name = "上传量进度监控"
    plugin_desc = "为每位用户绑定站点和累计上传目标，每小时汇总待完成、已完成及查询失败的任务。"
    plugin_icon = "https://raw.githubusercontent.com/bctjo/MoviePilot-Plugins/main/plugins.v3/uploadprogressmonitor/icon.svg"
    plugin_version = "0.1.1"
    plugin_author = "bctjo"
    author_url = "https://github.com/bctjo"
    plugin_config_prefix = "uploadprogressmonitor_"
    plugin_order = 100
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._lock, self._stop = Lock(), Event()
        self._config, self._tasks = deepcopy(DEFAULTS), []
        self._config_error = self._notice = None

    def init_plugin(self, config=None):
        """重配不打断已启动的一轮；停用或重载会通知当前任务停止。"""
        supplied = config or {}
        current = {key: supplied.get(key, deepcopy(value)) for key, value in DEFAULTS.items()}
        self._config_error = self._notice = None
        try:
            CronTrigger.from_crontab(current["cron"], timezone=TZ)
            tasks = parse_tasks(current)
            if current["enabled"] and not tasks:
                raise DataError("请至少添加一个完整且启用的监控任务")
        except DataError as error:
            tasks, self._config_error = [], str(error)
        except (ValueError, TypeError):
            tasks, self._config_error = [], "检查周期无效，请填写五位 cron 表达式"
        self._config, self._tasks = current, tasks
        if not self.get_state():
            self._stop.set()
        elif self._stop.is_set():
            self._stop = Event()
        self._clear_once_job()
        if current["onlyonce"]:
            self._config["onlyonce"] = False
            self.update_config(deepcopy(self._config))
            if self.get_state():
                try:
                    added = scheduler_sdk.add_plugin_once_job(self.__class__.__name__, "check_once", self.run_check,
                                                              "上传量立即检查", delay_seconds=3)
                except Exception as error:
                    added = False
                    logger.warning(f"上传量进度监控：一次性任务调度异常（{type(error).__name__}）")
                if not added:
                    self._notice = "立即检查未能加入宿主调度器，请稍后重试"
                    logger.warning("上传量进度监控：" + self._notice)
        logger.info(f"上传量进度监控：配置已保存，启用={self.get_state()}，任务={len(tasks)}，推送={bool(current['notify'])}")
        if self._config_error:
            logger.warning("上传量进度监控：" + self._config_error)

    def get_state(self):
        return bool(self._config["enabled"] and not self._config_error)

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        return []

    def get_service(self):
        """默认每小时整点检查，调度器由宿主管理。"""
        if not self.get_state():
            return []
        return [{"id": "UploadProgressMonitor.Check", "name": self.plugin_name,
                 "trigger": CronTrigger.from_crontab(self._config["cron"], timezone=TZ),
                 "func": self.run_check, "kwargs": {}}]

    def stop_service(self):
        self._stop.set()
        self._clear_once_job()
        logger.info("上传量进度监控：停止请求已记录，当前网络请求结束后退出")

    def _clear_once_job(self):
        """宿主正在停止时，清理失败也不阻止插件记录停止信号。"""
        try:
            scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, "check_once")
        except Exception as error:
            logger.warning(f"上传量进度监控：一次性任务清理异常（{type(error).__name__}）")

    @staticmethod
    def _now():
        return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    def _persist(self, result, stage):
        result["stage"], result["updated_at"] = stage, self._now()
        self.save_data("last_result", result)
        logger.info("上传量进度监控：" + stage)

    @staticmethod
    def _html(response):
        """优先使用页面声明的编码，兼容 NexusPHP 的旧中文页面。"""
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            if len(content) > 5_000_000:
                raise DataError("用户详情页超过解析大小限制")
            match = re.search(br'charset\s*=\s*["\x27]?([A-Za-z0-9_-]+)', content[:4096], re.I)
            encoding = match.group(1).decode("ascii") if match else "utf-8"
            try:
                return content.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                raise DataError("用户详情页编码无法识别") from None
        body = getattr(response, "text", None)
        if not isinstance(body, str):
            raise DataError("站点未返回有效的用户详情页")
        return body

    def _read_user(self, site, uid):
        """请求只查询资料，不跟随重定向，不记录凭据和原始页面。"""
        adapter = adapter_for(site)
        headers = {"User-Agent": site.ua or "MoviePilot/UploadProgressMonitor"}
        options = {"headers": headers, "proxies": settings.PROXY if site.proxy else None,
                   "timeout": 30, "verify": True}
        if adapter == "mteam":
            if not site.apikey:
                raise DataError("该馒头站点未配置 API Key")
            headers.update({"x-api-key": site.apikey, "Accept": "application/json"})
        else:
            parsed = urlparse(str(site.url or ""))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise DataError("请在 MP 站点管理中配置有效的 HTTPS 站点地址")
            if not site.cookie:
                raise DataError("该站点未配置 Cookie，请先在 MP 站点管理中配置")
            options["cookies"] = site.cookie
            headers["Accept"] = "text/html"
        started = monotonic()
        logger.info(f"上传量进度监控：开始查询站点 ID {site.id}，UID {uid}（超时 30 秒）")
        try:
            client = RequestUtils(**options)
            response = (client.post_res(API_BASE + "/member/profile", params={"uid": uid}, allow_redirects=False)
                        if adapter == "mteam" else
                        client.get_res(urljoin(str(site.url).rstrip("/") + "/", "userdetails.php"),
                                       params={"id": uid}, allow_redirects=False))
        except Exception as error:
            raise DataError(f"网络请求异常（{type(error).__name__}）") from None
        logger.info(f"上传量进度监控：站点 ID {site.id}，UID {uid}，HTTP {response.status_code if response is not None else '无响应'}，耗时 {monotonic() - started:.1f} 秒")
        if response is None:
            raise DataError("站点请求失败或超时")
        if response.status_code != 200:
            raise DataError(f"站点返回 HTTP {response.status_code}，请检查地址、Cookie 或访问限制")
        if adapter == "nexusphp":
            return parse_nexus_profile(self._html(response), uid)
        try:
            payload = response.json()
        except ValueError:
            raise DataError("馒头 API 未返回有效 JSON") from None
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise DataError("馒头 API 拒绝请求，请检查该站点 API Key 和资料访问权限")
        return parse_mteam_profile(payload.get("data"), uid)

    def run_check(self):
        """每轮独立查询全部启用任务，完成任务也继续参与后续小时汇总。"""
        if not self.get_state() or not self._lock.acquire(blocking=False):
            logger.info("上传量进度监控：未启用、配置无效或已有任务运行，本次跳过")
            return
        tasks, config, stop = deepcopy(self._tasks), deepcopy(self._config), self._stop
        started = monotonic()
        result = {"status": "running", "rows": [], "total": len(tasks), "error": None,
                  "notification": "尚未推送", "updated_at": self._now()}
        try:
            self._persist(result, "开始检查上传量")
            self._scan(result, tasks, stop)
            if not stop.is_set() and config["notify"]:
                messages = notification_texts(result)
                for index, message in enumerate(messages, 1):
                    if stop.is_set():
                        break
                    self._persist(result, f"正在提交 MP 进度汇总 {index}/{len(messages)}")
                    self.post_message(mtype=MessageType.SiteMessage, title="上传量进度监控", text=message)
                    result["notification"] = f"已提交 MP 通知渠道 {index}/{len(messages)} 批（不代表收件端送达）"
            elif not config["notify"]:
                result["notification"] = "预览模式：未推送"
        except DataError as error:
            result["error"] = str(error)
        except Exception as error:
            result["error"] = f"本轮任务异常（{type(error).__name__}）"
        finally:
            try:
                result["status"] = "stopped" if stop.is_set() else "failed" if result["error"] else "completed"
                result["elapsed_seconds"] = round(monotonic() - started, 1)
                if stop.is_set():
                    result["error"] = "本轮已停止，未检查的任务不计入已完成"
                self._persist(result, f"本轮结束，已检查 {len(result['rows'])}/{len(tasks)} 项，耗时 {result['elapsed_seconds']} 秒")
            finally:
                self._lock.release()

    def _scan(self, result, tasks, stop):
        """按任务绑定的站点逐人查询，一个用户失败不阻断其它用户。"""
        sites = {str(site.id): site for site in SiteOper().list_order_by_pri()}
        previous = self.get_data("last_success") or {}
        if not isinstance(previous, dict):
            previous = {}
        states = {task["key"]: previous[task["key"]] for task in tasks if task["key"] in previous}
        for index, task in enumerate(tasks, 1):
            if stop.is_set() or (index > 1 and stop.wait(REQUEST_INTERVAL)):
                return
            site = sites.get(str(task["site_id"]))
            old = previous.get(task["key"], {})
            if not isinstance(old, dict):
                old = {}
            row = dict(task, site_name=label(site.name) if site else f"站点 ID {task['site_id']}",
                       status="error", error=None, checked_at=self._now())
            self._persist(result, f"正在查询 {index}/{len(tasks)}：{row['site_name']}，UID {task['uid']}")
            try:
                if site is None:
                    raise DataError("绑定站点不存在，请在任务配置中重新选择")
                sample = self._read_user(site, task["uid"])
                row.update(sample)
                row.update(progress(sample, task["target_bytes"]))
                if old.get("uploaded_bytes") is not None and not old.get("approximate") and not sample["approximate"]:
                    row["delta_bytes"] = sample["uploaded_bytes"] - old["uploaded_bytes"]
                    if row["delta_bytes"] < 0:
                        row["warning"] = "累计上传量较上次下降，请核实站点统计"
                row["completed_at"] = ((old.get("completed_at") if old.get("target_bytes") == task["target_bytes"] else None)
                                       or self._now()) if row["status"] == "completed" else None
                states[task["key"]] = {key: row.get(key) for key in ("username", "uploaded_bytes", "approximate", "target_bytes", "completed_at")}
                states[task["key"]]["checked_at"] = row["checked_at"]
            except DataError as error:
                row["status"] = "error"
                row["error"] = str(error)
            except Exception as error:
                row["status"] = "error"
                row["error"] = f"查询异常（{type(error).__name__}）"
            if row["status"] == "error" and old.get("uploaded_bytes") is not None:
                row.update(last_uploaded_bytes=old["uploaded_bytes"], last_success_at=old.get("checked_at", "—"))
            result["rows"].append(row)
            self.save_data("last_success", states)
            state_label = {"pending": "待完成", "completed": "已完成", "error": "查询失败"}[row["status"]]
            self._persist(result, f"已检查 {index}/{len(tasks)}：{row['site_name']}，UID {task['uid']}，{state_label}")

    def get_form(self):
        sites = []
        for site in SiteOper().list_order_by_pri():
            host = (urlparse(str(site.url)).hostname or "").lower()
            kind = "馒头 API" if adapter_for(site) == "mteam" else "Audiences" if host == "audiences.me" or host.endswith(".audiences.me") else "通用 NexusPHP"
            sites.append({"title": f"{label(site.name)} · {kind}", "value": str(site.id)})
        return config_form(sites, self._config_error or self._notice), deepcopy(DEFAULTS)

    def get_page(self):
        return details_page(self.get_data("last_result") or {}, self._config_error or self._notice)
