"""User-local settings. Private user credentials stay outside the checkout."""
import json
import hashlib
from copy import deepcopy
from datetime import datetime
from dataclasses import dataclass
import os
import tempfile
import threading
from functools import wraps
from uuid import uuid4
from pathlib import Path

from ai.client import AIConfig
from ai.defaults import DEFAULT_PROFILE_ID, DEFAULT_PROVIDER_NAME
from base.concurrency import INTERACTIVE_WORKERS, validate_workers


_mutation_lock = threading.RLock()


def _transaction(method):
    """Serialize complete read/modify/write operations, including background tests."""
    @wraps(method)
    def locked(*args, **kwargs):
        with _mutation_lock:
            return method(*args, **kwargs)
    return locked


class SettingsError(RuntimeError):
    pass


@dataclass
class ModelProfile:
    id: str
    config: AIConfig
    active: bool = False

    @property
    def builtin(self):
        return self.id == DEFAULT_PROFILE_ID


def config_directory():
    override = os.environ.get("ICVE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "icve-ai-assistant"
    return Path.home() / ".config" / "icve-ai-assistant"


class SettingsStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else config_directory()
        self.path = self.directory / "settings.json"

    def _read(self):
        try:
            if not self.path.exists():
                return {}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version", 1) != 1:
                raise ValueError()
            return data
        except (OSError, ValueError):
            raise SettingsError(f"无法读取本机配置，请检查 {self.path}（原文件未覆盖）") from None

    def get(self, key, default=None):
        return self._read().get(key, default)

    @_transaction
    def set(self, key, value):
        data = self._read()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        self._write(data)

    def concurrency(self):
        saved = self.get("concurrency", {})
        saved = saved if isinstance(saved, dict) else {}
        result = {}
        for key in ("ai", "study"):
            try:
                result[key] = validate_workers(saved.get(key, INTERACTIVE_WORKERS))
            except ValueError:
                result[key] = INTERACTIVE_WORKERS
        return result

    def save_concurrency(self, ai, study):
        try:
            options = {"ai": validate_workers(ai), "study": validate_workers(study)}
        except ValueError as exc:
            raise SettingsError(str(exc)) from None
        self.set("concurrency", options)

    def _write(self, data):
        data["version"] = 1
        temporary = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=self.directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise SettingsError("无法保存本机配置，请检查配置目录权限；旧配置未被覆盖") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def _profiles(self, data):
        """Validate or expose legacy data without writing during a read."""
        try:
            profiles = data.get("ai_profiles")
            if profiles is None:
                old = data.get("ai")
                if old is None:
                    return {"active_id": None, "items": []}
                profiles = {"active_id": "legacy", "items": [dict(old, id="legacy",
                              health=data.get("ai_health", {}))]}
            if not isinstance(profiles, dict) or not isinstance(profiles.get("items"), list):
                raise ValueError()
            ids = set()
            for item in profiles["items"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or item["id"] in ids:
                    raise ValueError()
                ids.add(item["id"])
                AIConfig(item["base_url"], item["model"], item["api_key"],
                         provider_name=item.get("provider_name", ""),
                         api_format=item.get("api_format", "chat_completions"))
            if profiles.get("active_id") is not None and profiles["active_id"] not in ids:
                raise ValueError()
            return profiles
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SettingsError("保存的模型配置无效，原文件未修改，请检查本机配置") from None

    def _save_profiles(self, data, profiles):
        data["ai_profiles"] = profiles
        data.pop("ai", None)
        data.pop("ai_health", None)
        self._write(data)

    @staticmethod
    def _profile_item(profiles, profile_id):
        item = next((item for item in profiles["items"] if item["id"] == profile_id), None)
        if item is None:
            raise SettingsError("模型配置已不存在，请重新选择")
        return item

    def ai_profiles(self):
        profiles = self._profiles(self._read())
        return [ModelProfile(item["id"], AIConfig(item["base_url"], item["model"], item["api_key"],
                         provider_name=item.get("provider_name", ""),
                         api_format=item.get("api_format", "chat_completions")),
                             profiles.get("active_id") == item["id"])
                for item in sorted(profiles["items"], key=lambda item: item["id"] != DEFAULT_PROFILE_ID)]

    def ai_config(self):
        return next((profile.config for profile in self.ai_profiles() if profile.active), None)

    @_transaction
    def ensure_default_model(self):
        """Keep one locked built-in provider, migrating the earlier editable entry."""
        from ai.defaults import default_config
        data = self._read()
        original = self._profiles(data)
        profiles = deepcopy(original)
        config = default_config()
        matching = next((item for item in profiles["items"] if item["id"] == DEFAULT_PROFILE_ID), None)
        if matching is None:
            matching = next((item for item in profiles["items"]
                         if (item["base_url"], item["model"], item["api_key"],
                             item.get("api_format", "chat_completions")) ==
                         (config.base_url, config.model, config.api_key, config.api_format)), None)
        if matching is None:
            if not profiles["items"]:
                profiles["active_id"] = DEFAULT_PROFILE_ID
            matching = {}
        else:
            if profiles.get("active_id") == matching["id"]:
                profiles["active_id"] = DEFAULT_PROFILE_ID
            profiles["items"].remove(matching)
        matching.update(id=DEFAULT_PROFILE_ID, base_url=config.base_url,
                        model=config.model, api_key=config.api_key, provider_name=DEFAULT_PROVIDER_NAME,
                        api_format="chat_completions")
        profiles["items"].insert(0, matching)
        if profiles == original and data.get("default_model_added"):
            return
        data["default_model_added"] = True
        self._save_profiles(data, profiles)

    @_transaction
    def save_ai_profile(self, config, profile_id=None):
        data = self._read()
        profiles = self._profiles(data)
        if profile_id is None:
            profile_id = uuid4().hex
            item = {"id": profile_id}
            if not profiles["items"]:
                profiles["active_id"] = profile_id
            profiles["items"].append(item)
        else:
            item = self._profile_item(profiles, profile_id)
            if profile_id == DEFAULT_PROFILE_ID:
                raise SettingsError("默认供应商不可修改")
        item.update(base_url=config.base_url, model=config.model, api_key=config.api_key,
                    provider_name=config.provider_name, api_format=config.api_format)
        health = item.get("health", {})
        if not isinstance(health, dict) or health.get("config") != self.ai_fingerprint(config):
            item.pop("health", None)
        self._save_profiles(data, profiles)
        return profile_id

    @_transaction
    def activate_ai_profile(self, profile_id):
        data = self._read()
        profiles = self._profiles(data)
        self._profile_item(profiles, profile_id)
        profiles["active_id"] = profile_id
        self._save_profiles(data, profiles)

    @_transaction
    def delete_ai_profile(self, profile_id):
        data = self._read()
        profiles = self._profiles(data)
        if profile_id == DEFAULT_PROFILE_ID:
            raise SettingsError("默认供应商不可删除")
        profiles["items"].remove(self._profile_item(profiles, profile_id))
        if profiles.get("active_id") == profile_id:
            profiles["active_id"] = None
        self._save_profiles(data, profiles)

    @_transaction
    def clear_ai(self):
        data = self._read()
        profiles = self._profiles(data)
        profiles["items"] = [item for item in profiles["items"] if item["id"] == DEFAULT_PROFILE_ID]
        if profiles.get("active_id") != DEFAULT_PROFILE_ID:
            profiles["active_id"] = None
        self._save_profiles(data, profiles)

    @_transaction
    def save_ai(self, config):
        current = next((profile.id for profile in self.ai_profiles() if profile.active), None)
        return self.save_ai_profile(config, current)

    @staticmethod
    def ai_fingerprint(config):
        identity = json.dumps([config.endpoint, config.model, config.api_key])
        if config.api_format == "auto":
            identity += ":auto"
        return hashlib.sha256(identity.encode()).hexdigest()

    def ai_health(self, config):
        profiles = self._profiles(self._read())
        for item in profiles["items"]:
            health = item.get("health", {})
            if isinstance(health, dict) and health.get("config") == self.ai_fingerprint(config):
                return health
        return {}

    @_transaction
    def save_ai_health(self, config, status, message="", *, previous_config=None):
        data = self._read()
        profiles = self._profiles(data)
        fingerprint = self.ai_fingerprint(config)
        previous_fingerprint = self.ai_fingerprint(previous_config) if previous_config else fingerprint
        for item in profiles["items"]:
            stored = AIConfig(item["base_url"], item["model"], item["api_key"],
                         provider_name=item.get("provider_name", ""),
                         api_format=item.get("api_format", "chat_completions"))
            if self.ai_fingerprint(stored) == previous_fingerprint:
                if status == "ok":
                    item["api_format"] = config.api_format
                item["health"] = {"config": fingerprint, "status": status, "message": message,
                                  "checked_at": datetime.now().isoformat(timespec="seconds")}
        self._save_profiles(data, profiles)

    def session(self, platform):
        data = self.get("session_" + platform)
        if isinstance(data, dict) and isinstance(data.get("access_token"), str) and data["access_token"]:
            return data
        return None

    def save_session(self, platform, client):
        self.set("session_" + platform, {"access_token": client.access_token})

    def clear_session(self, platform):
        self.set("session_" + platform, None)
