"""模块市场(用户版)

功能:
    sun market list                 列出已安装模块
    sun market find <关键词>        搜索共享 Release 中的模块
    sun market install <name/ID>    安装或更新模块
    sun market mkid <name/ID>       生成或更新 ID
    sun market used <name/ID>       启用模块
    sun market unused <name/ID>     弃用模块
    sun market rm <name/ID>         删除本地模块
"""

import io
import json
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_RELEASE_TAG = "mods"
DEFAULT_INDEX_ASSET = "index.json"
CONFIRM_EXIT_CODE = 3


class Module:
    name = "market"
    role = "market"
    desc = "模块市场: 列出、搜索、安装、启停和删除本地模块"
    subcommands = [
        "help", "list", "find", "install", "mkid", "used", "unused", "rm",
    ]

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def call(self):
        """初始化模块临时目录(项目根目录 tmp/)"""
        tmp = self._tmp_dir()
        for child in tmp.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # 命令
    # ------------------------------------------------------------------ #

    def run(self):
        """sun market: 显示帮助"""
        self.help()

    def help(self):
        """显示 market 帮助"""
        print(self.desc)
        print("用法:")
        print("  sun market list                     列出已安装模块")
        print("  sun market find <关键词>            搜索共享 Release 中的模块")
        print("  sun market install <name/ID>        安装或更新模块(必要时确认)")
        print("  sun market mkid <name/ID>           生成或更新模块 ID")
        print("  sun market used <name/ID>           启用模块")
        print("  sun market unused <name/ID>         弃用模块")
        print("  sun market rm <name/ID>             删除本地模块目录")
        print()
        print("配置:")
        print(f"  {self._market_json()}")
        print("  gitee.repo / gitee.release_tag")

    def list(self):
        """sun market list: 列出已安装模块"""
        records = self._dedup(self._scan_local())
        if not records:
            print("(没有已安装的模块)")
            return
        for rec in records:
            self._print_record(rec)
            print()

    def find(self, keyword=""):
        """sun market find <关键词>: 搜索远程模块"""
        keyword = str(keyword).strip()
        if not keyword:
            self._fail("用法: sun market find <name/role/ID/关键词>")
        try:
            cfg = self._remote_config()
            index = self._fetch_index(cfg)
        except RuntimeError as e:
            cached = self._read_json(self._cache_path())
            if isinstance(cached.get("modules"), list):
                print(f"警告: 云端访问失败, 使用本地缓存: {e}", file=sys.stderr)
                index = cached
            else:
                self._fail(str(e))

        modules = index.get("modules", []) if isinstance(index, dict) else []
        if not isinstance(modules, list):
            modules = []
        records = self._dedup([r for r in modules if isinstance(r, dict)])
        needle = keyword.lower()
        matched = []
        for rec in records:
            haystack = " ".join(
                str(rec.get(key, "")) for key in ("name", "role", "ID", "desc")
            )
            if needle in haystack.lower():
                matched.append(rec)
        if not matched:
            print(f"没有匹配的模块: {keyword}")
            return
        for rec in matched:
            self._print_record(rec)
            print()

    def install(self, target="", confirm=""):
        """sun market install <name/ID>: 安装或更新模块"""
        target = str(target or "").strip()
        if not target:
            self._fail("用法: sun market install <name/ID>")
        try:
            cfg = self._remote_config()
            release, index, assets = self._load_release_index(cfg)
        except RuntimeError as e:
            self._fail(str(e))

        modules = index.get("modules", [])
        if not isinstance(modules, list):
            modules = []
        records = [r for r in modules if isinstance(r, dict)]
        rec = self._resolve_remote(records, target)

        name = rec["name"]
        target_dir = self._modules_dir() / name
        if target_dir.exists():
            old_manifest = self._read_json(target_dir / f"{name}.json")
            old_id = str(old_manifest.get("ID") or "")
            remote_ids = {str(item.get("ID") or "") for item in records}
            if old_id not in remote_ids:
                confirmed = str(confirm).strip().lower() in ("yes", "--yes", "-y")
                if not confirmed:
                    print(f"本地已有模块: {name} ({old_id or '无 ID'})")
                    print("该 ID 不在云端，替换后旧模块将被移除。")
                    raise SystemExit(CONFIRM_EXIT_CODE)

        asset_name = str(rec.get("asset") or f"{rec['ID']}.zip")
        asset = self._find_asset(assets, asset_name)
        if asset is None:
            self._fail(f"Release 中找不到资产: {asset_name}")

        try:
            data = self._download_asset(cfg, asset)
            installed = self._install_zip(rec, data)
        except RuntimeError as e:
            self._fail(str(e))

        print(f"已安装: {rec['name']} ({rec['ID']})")
        print(f"路径: {installed}")
        print("重启 core 后生效: sun stop")

    def mkid(self, target=""):
        """sun market mkid <name/ID>: 生成或更新 ID"""
        rec = self._resolve(target)
        new_id = f"{rec['name']}{int(time.time())}"
        manifest = self._read_json(rec["json_path"])
        manifest["name"] = rec["name"]
        manifest["ID"] = new_id
        manifest.setdefault("used", rec["used"])
        self._write_json(rec["json_path"], manifest)
        self._sync_instance(rec, ID=new_id)
        print(f"已更新 ID: {rec['name']} -> {new_id}")
        print("重启 core 后新 ID 才会参与同 role 替换。")

    def used(self, target=""):
        """sun market used <name/ID>: 启用模块"""
        self._set_used(target, True)

    def unused(self, target=""):
        """sun market unused <name/ID>: 弃用模块"""
        self._set_used(target, False)

    def rm(self, target=""):
        """sun market rm <name/ID>: 删除本地模块"""
        rec = self._resolve(target)
        modules_dir = self._modules_dir().resolve()
        path = rec["dir"].resolve()
        if path == modules_dir or modules_dir not in path.parents:
            self._fail(f"拒绝删除: {path} 不在 modules/ 下")
        was_self = self._is_self(rec)
        shutil.rmtree(path)
        self._drop_instance(rec)
        print(f"已删除: {rec['name']} ({path})")
        if was_self:
            print("market 自身已删除; 重启 core 后 market 将不再加载。")

    # ------------------------------------------------------------------ #
    # 本地模块
    # ------------------------------------------------------------------ #

    def _modules_dir(self):
        return Path(G.APP_PATH) / "modules"

    def _market_json(self):
        return Path(__file__).with_suffix(".json")

    def _tmp_dir(self):
        tmp = Path(G.APP_PATH) / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp

    def _cache_path(self):
        return self._tmp_dir() / "index.json"

    def _save_cache(self, index):
        try:
            self._write_json(self._cache_path(), index)
        except Exception:
            pass

    def _read_json(self, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_json(self, path, data):
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )

    def _scan_local(self):
        modules_dir = self._modules_dir()
        records = []
        if not modules_dir.is_dir():
            return records

        for directory in sorted(modules_dir.iterdir()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            if not (directory / f"{directory.name}.py").is_file():
                continue

            json_path = directory / f"{directory.name}.json"
            manifest = self._read_json(json_path)
            name = str(manifest.get("name") or directory.name).strip()
            role = str(manifest.get("role") or "").strip()
            module_id = str(manifest.get("ID") or "").strip()
            desc = str(manifest.get("desc") or manifest.get("help") or "").strip()
            used = manifest.get("used")
            if not isinstance(used, bool):
                used = None

            inst = self._find_instance(name=name, role=role)
            if inst is not None:
                name = name or str(getattr(inst, "name", "") or "")
                role = role or str(getattr(inst, "role", "") or "")
                module_id = module_id or str(getattr(inst, "ID", "") or "")
                desc = desc or str(getattr(inst, "desc", "") or "")
                if used is None:
                    used = getattr(inst, "_used", True)

            records.append({
                "name": name,
                "role": role,
                "ID": module_id,
                "desc": desc,
                "used": True if used is None else bool(used),
                "dir": directory,
                "json_path": json_path,
                "manifest": manifest,
            })
        return records

    def _find_instance(self, name="", role=""):
        for mod in getattr(G, "MODULES_LIST", []):
            if role and getattr(mod, "role", None) != role:
                continue
            if name and getattr(mod, "name", None) != name:
                continue
            if role or name:
                return mod
        return None

    def _resolve(self, target):
        target = str(target or "").strip()
        if not target:
            self._fail("缺少模块: 用法 sun market <命令> <name/ID>")

        records = self._scan_local()
        exact = [
            rec for rec in records
            if target in (rec["name"], rec["role"], rec["ID"], rec["dir"].name)
        ]
        if not exact:
            needle = target.lower()
            exact = [
                rec for rec in records
                if needle in (
                    rec["name"].lower(),
                    rec["role"].lower(),
                    rec["ID"].lower(),
                    rec["dir"].name.lower(),
                )
            ]
        if not exact:
            self._fail(f"找不到模块: {target}")

        for key in ("name", "ID", "role"):
            for rec in exact:
                if rec[key] == target:
                    return rec
        for rec in exact:
            if rec["dir"].name == target:
                return rec
        return exact[0]

    def _resolve_remote(self, records, target):
        target = str(target or "").strip()
        exact = [
            rec for rec in records
            if target == str(rec.get("name") or "")
            or target == str(rec.get("ID") or "")
        ]
        if not exact:
            lower = target.lower()
            exact = [
                rec for rec in records
                if lower == str(rec.get("name") or "").lower()
            ]
        if not exact:
            self._fail(f"远程找不到模块: {target}")
        exact.sort(key=lambda rec: self._id_ts(rec.get("ID")), reverse=True)
        return exact[0]

    def _dedup(self, records):
        best = {}
        for rec in records:
            name = str(rec.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            current = best.get(key)
            if current is None or self._id_ts(rec.get("ID")) > self._id_ts(current.get("ID")):
                best[key] = rec
        return sorted(best.values(), key=lambda rec: str(rec.get("name", "")).lower())

    def _print_record(self, rec):
        print(f"name: {rec.get('name') or '-'}")
        print(f"role: {rec.get('role') or '-'}")
        print(f"ID: {rec.get('ID') or '-'}")
        used = rec.get("used")
        if isinstance(used, bool):
            print(f"used: {'true' if used else 'false'}")
        print(f"desc: {rec.get('desc') or '-'}")

    def _sync_instance(self, rec, ID=None, used=None):
        inst = self._find_instance(name=rec["name"], role=rec["role"])
        if inst is None:
            return False
        if ID is not None:
            inst.ID = ID
        if used is not None:
            inst._used = used
            raw = getattr(inst, "used", None)
            if not callable(raw):
                inst.used = used
        return True

    def _set_used(self, target, flag):
        rec = self._resolve(target)
        manifest = self._read_json(rec["json_path"])
        manifest.setdefault("name", rec["name"])
        manifest["used"] = flag
        self._write_json(rec["json_path"], manifest)

        synced = self._sync_instance(rec, used=flag)
        state = "启用" if flag else "弃用"
        print(f"已{state}: {rec['name']} (used={'true' if flag else 'false'})")
        if not synced:
            print("已写入 JSON; 当前 core 未找到该模块实例, 重启后按模块自身 call() 逻辑生效。")
        if self._is_self(rec) and not flag:
            print("警告: market 已弃用自身; 之后 sun market 不可用。")
            print("恢复方式: 手动把 market.json 的 used 改为 true, 然后重启 core。")

    def _drop_instance(self, rec):
        targets = []
        for mod in list(getattr(G, "MODULES_LIST", [])):
            role_ok = rec["role"] and getattr(mod, "role", None) == rec["role"]
            name_ok = getattr(mod, "name", None) == rec["name"]
            if role_ok or name_ok:
                targets.append(mod)

        for mod in targets:
            try:
                G.MODULES_LIST.remove(mod)
            except ValueError:
                pass
        for token, mod in list(getattr(G, "by_name", {}).items()):
            if mod in targets:
                G.by_name.pop(token, None)
        for token, mod in list(getattr(G, "by_role", {}).items()):
            if mod in targets:
                G.by_role.pop(token, None)
        for token in list(getattr(G, "_module_attrs", set())):
            if getattr(G, token, None) in targets:
                try:
                    delattr(G, token)
                except Exception:
                    pass
                G._module_attrs.discard(token)

    def _is_self(self, rec):
        try:
            return rec["dir"].resolve() == Path(__file__).resolve().parent
        except Exception:
            return False

    @staticmethod
    def _id_ts(value):
        match = re.search(r"(\d+)$", str(value or ""))
        return int(match.group(1)) if match else -1

    @staticmethod
    def _id_ok(name, value):
        value = str(value or "")
        return bool(name) and value.startswith(name) and value[len(name):].isdigit()

    @staticmethod
    def _fail(message):
        print(message, file=sys.stderr)
        raise SystemExit(1)

    # ------------------------------------------------------------------ #
    # Gitee Release
    # ------------------------------------------------------------------ #

    def _remote_config(self):
        data = self._read_json(self._market_json())
        raw = data.get("gitee")
        cfg = raw if isinstance(raw, dict) else {}

        repo = str(cfg.get("repo") or "").strip()
        repo = re.sub(r"^(https?://)?(www\.)?gitee\.com/", "", repo, flags=re.I)
        repo = repo.strip().strip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        if not repo or repo.count("/") != 1:
            raise RuntimeError(
                "Gitee 仓库未配置: 请在 market.json 的 gitee.repo 填写 owner/repo"
            )

        return {
            "repo": repo,
            "token": "",
            "release_tag": str(cfg.get("release_tag") or DEFAULT_RELEASE_TAG),
            "index_asset": str(cfg.get("index_asset") or DEFAULT_INDEX_ASSET),
        }

    @staticmethod
    def _api_url(cfg, path):
        return f"https://gitee.com/api/v5/repos/{cfg['repo']}/{path.lstrip('/')}"

    def _http_json(self, url, cfg):
        headers = {
            "Accept": "application/json",
            "User-Agent": "sunday-market",
        }

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"Gitee API {e.code}: {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接 Gitee: {e.reason}") from e

        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError("Gitee 返回了非 JSON 数据") from e

    def _get_release(self, cfg):
        tag = urllib.parse.quote(cfg["release_tag"], safe="")
        url = self._api_url(cfg, f"releases/tags/{tag}")
        try:
            release = self._http_json(url, cfg)
        except RuntimeError as e:
            if "404" not in str(e):
                raise
            release = None

        if isinstance(release, dict) and release.get("id"):
            return release
        raise RuntimeError(f"找不到共享 Release: tag={cfg['release_tag']}")

    def _release_assets(self, cfg, release):
        release_id = release.get("id")
        if not release_id:
            raise RuntimeError("Gitee Release 缺少 id")
        url = self._api_url(cfg, f"releases/{release_id}/attach_files")
        try:
            data = self._http_json(url, cfg)
        except RuntimeError:
            assets = release.get("assets")
            if isinstance(assets, list):
                return assets
            raise
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("attach_files"), list):
                return data["attach_files"]
            if isinstance(data.get("assets"), list):
                return data["assets"]
        return []

    @staticmethod
    def _find_asset(assets, name):
        matches = [item for item in assets if item.get("name") == name]
        if not matches:
            return None

        def key(item):
            value = item.get("id") or item.get("attach_file_id") or ""
            try:
                return (1, int(value))
            except (TypeError, ValueError):
                return (0, str(value))

        return max(matches, key=key)

    def _load_release_index(self, cfg):
        release = self._get_release(cfg)
        assets = self._release_assets(cfg, release)
        index_name = cfg["index_asset"]
        asset = self._find_asset(assets, index_name)
        if asset is None:
            raise RuntimeError(
                f"共享 Release 里还没有 {index_name}; 请联系维护者上传模块"
            )
        raw = self._download_asset(cfg, asset)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{index_name} 不是有效 JSON") from e
        index = data if isinstance(data, dict) else {"modules": []}
        if not isinstance(index.get("modules"), list):
            index["modules"] = []
        self._save_cache(index)
        return release, index, assets

    def _fetch_index(self, cfg):
        _, index, _ = self._load_release_index(cfg)
        return index

    def _download_asset(self, cfg, asset):
        url = (
            asset.get("browser_download_url")
            or asset.get("download_url")
            or asset.get("url")
        )
        if not url:
            raise RuntimeError(f"资产缺少下载地址: {asset.get('name')}")
        headers = {
            "Accept": "application/octet-stream",
            "User-Agent": "sunday-market",
        }
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"下载资产失败 {asset.get('name')}: HTTP {e.code}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"下载资产失败: {e.reason}") from e

    def _install_zip(self, rec, data):
        name = str(rec.get("name") or "").strip()
        expected_id = str(rec.get("ID") or "").strip()
        if (
            not name
            or name in (".", "..")
            or name.startswith("_")
            or "/" in name
            or "\\" in name
        ):
            raise RuntimeError(f"远程模块名非法: {name!r}")
        if not self._id_ok(name, expected_id):
            raise RuntimeError(f"远程 ID 格式不正确: {expected_id or '(空)'}")

        modules_dir = self._modules_dir()
        modules_dir.mkdir(parents=True, exist_ok=True)
        target = modules_dir / name
        backup = modules_dir / f".{name}.bak"

        # 安装自身时临时目录不能放在被替换的目录里
        tmp_parent = self._modules_dir() if name == self.name else self._tmp_dir()
        with tempfile.TemporaryDirectory(
            prefix=".install-", dir=tmp_parent
        ) as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for member in zf.infolist():
                        filename = member.filename
                        member_path = Path(filename)
                        if (
                            member_path.is_absolute()
                            or filename.startswith("/")
                            or ".." in member_path.parts
                        ):
                            raise RuntimeError(f"压缩包包含非法路径: {filename}")
                        mode = member.external_attr >> 16
                        if stat.S_ISLNK(mode):
                            raise RuntimeError(f"压缩包包含符号链接: {filename}")
                        zf.extract(member, tmp)
            except zipfile.BadZipFile as e:
                raise RuntimeError("下载的资产不是有效 zip") from e

            src = tmp / name
            py_file = src / f"{name}.py"
            json_file = src / f"{name}.json"
            if not py_file.is_file() or not json_file.is_file():
                raise RuntimeError(
                    f"zip 结构不正确, 需要 {name}/{name}.py 和 {name}/{name}.json"
                )

            manifest = self._read_json(json_file)
            if str(manifest.get("ID") or "") != expected_id:
                raise RuntimeError(
                    f"ID 校验失败: 包内 {manifest.get('ID') or '(空)'} != 索引 {expected_id}"
                )
            if manifest.get("name") and str(manifest.get("name")) != name:
                raise RuntimeError(
                    f"name 校验失败: 包内 {manifest.get('name')} != {name}"
                )

            if target.exists():
                if backup.exists():
                    shutil.rmtree(backup)
                target.rename(backup)
            try:
                shutil.move(str(src), str(target))
            except Exception:
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                if backup.exists():
                    backup.rename(target)
                raise
            if backup.exists():
                shutil.rmtree(backup)

        return target
