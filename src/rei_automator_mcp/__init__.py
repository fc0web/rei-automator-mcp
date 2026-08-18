#!/usr/bin/env python3
"""
rei-automator-mcp : PC 自動化操作を提案→承認→実行する MCP サーバー

AI エージェント (Claude Desktop / Cursor / Cline など) から Windows PC を
操作するための独立 MCP サーバー。 Rei-AIOS 内部の WorkspaceAutomator を
外部 MCP client からも直接呼べる形に抽出した。

設計方針
  - propose → approve → execute の 3 段 lifecycle (副作用の暴発防止)
  - 実行履歴を JSON で永続化 (次回起動時に load、 STEP 1336 pattern)
  - D-FUMT₈ 8 値 dfumtValue を tool 応答に併記 (Rei-AIOS 互換)
  - 12 action kinds (shell/file/UI/業務) 網羅、 但し MVP では 一部 stub
  - 日本語 IME bypass: `type` action は rei-sendinput.ps1 経由
    (Phase 1.5、 SendInput + KEYEVENTF_UNICODE で IME 層 skip)
  - Windows-first。 他 OS は Phase 2+ で 検討

Version: 0.2.0a3 (2026-08-19) — IME バイパス smoke test cover + 実地 GUI test script + Windows cp932 fix。
        週 1 ship pace 第 1 弾 (hybrid = research primary + AI が使いやすい道具 5 条件 chat-Claude 2026-08-18)。
        selftest [10] section 追加 (10 assertion、 total 30→40 PASS): PS1 script primitives
        (KEYEVENTF_UNICODE + SendInput + VK_RETURN + ExpectedHwnd/FOREGROUND_MISMATCH) と
        Python 経路 (Base64 UTF-8 transport + -ExpectedHwnd 伝搬 + foreground_mismatch 検出) の
        静的 verify + Japanese Unicode propose/approve/cancel lifecycle。 実 PS1 実行なし
        (Windows GUI 対象、 別 script で cover)。 scripts/manual-ime-test.py 新規 (実地
        Notepad IME バイパス GUI テスト、 --dry-run / --text / --mismatch-hwnd の 3 mode)。
        manual-ime-test.py に Windows cp932 terminal 対策 (sys.stdout.reconfigure UTF-8、
        grounded v0.1.1 と同型 pitfall)。 README + docs 更新。
Version: 0.2.0a2 (2026-08-19) — type action foreground guard 追加 (「a3 hardening」 label は misnamed、
        実 version は a2)。 target_hwnd + foreground_retry_ms parameters を Action / propose() /
        _type_via_ps1() に導入、 rei-sendinput.ps1 に -ExpectedHwnd / -ForegroundRetryMs 追加、
        SendInput 直前に GetForegroundWindow() == ExpectedHwnd を verify、 不一致で exit 3 +
        FOREGROUND_MISMATCH stderr を返して 送信 abort。 a1 実測で発生した 誤 window (Claude Code
        chat) 流入事故 (\n が Enter=送信 として発火) を 構造的に阻止。 target_hwnd 未指定時は
        legacy 動作 (backward compat、 但し warning 表示)。
Version: 0.2.0a1 (2026-08-18) — Phase 2 accessibility API 統合開始 + PyPI publish 準備。
        find_element (旧 stub の search を pywinauto uia backend で本実装) を追加、
        click / type SetValue 経路 / screenshot / excel_aggregate は chat-Claude 分担で
        順次追加予定 (`docs/phase2-backend-design.md` 参照)。
        pyproject.toml 導入 + src/rei_automator_mcp/ layout + Trusted Publisher (OIDC) publish。
Version: 0.2.0-alpha (2026-08-15) — 初版 alpha、 Phase 2 accessibility API 統合開始
Version: 0.1.0 (2026-08-15) — 初版、 rei-aios STEP 1336 直後の 独立抽出

License: MIT (v0.x)。 v1.0+ で AGPL-3.0 + commercial dual への 切替可能性。
"""

from __future__ import annotations

import base64
import inspect
import json
import locale
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 保存先 + PS1 script location
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get(
    "REI_AUTOMATOR_DATA_DIR",
    Path.home() / ".rei-automator-mcp",
))
LOG_PATH = DATA_DIR / "execution-log.json"

# rei-sendinput.ps1 の bundled path (script と 同 dir に配置)
PS1_PATH = Path(__file__).parent / "rei-sendinput.ps1"


# ---------------------------------------------------------------------------
# 7 値論理 (D-FUMT₈ 8 値、 Rei-AIOS 互換)
# ---------------------------------------------------------------------------

DFUMT_TRUE = "TRUE"
DFUMT_FALSE = "FALSE"
DFUMT_BOTH = "BOTH"
DFUMT_NEITHER = "NEITHER"
DFUMT_INFINITY = "INFINITY"
DFUMT_ZERO = "ZERO"
DFUMT_FLOWING = "FLOWING"
DFUMT_SELF = "SELF"


# ---------------------------------------------------------------------------
# Action kind + Action dataclass
# ---------------------------------------------------------------------------

ACTION_KINDS = frozenset({
    "shell_command", "file_read", "file_write",
    "screenshot", "open", "search", "click", "type", "wait",
    "note_export", "excel_aggregate", "report", "proof_run",
    # Phase 2 (2026-08-15〜) — accessibility API 統合。 chat-Claude 設計書
    # `docs/phase2-backend-design.md` §2-3 提案 (search 改名 → find_element)。
    "find_element",
})


@dataclass
class Action:
    id: str
    kind: str
    label: str
    command: str
    approved: bool = False
    logic_basis: str = ""
    source_module: str = "mcp"
    result: str | None = None
    executed_at: float | None = None  # unix seconds
    # a3 (2026-08-18) — foreground safety for type action.
    # target_hwnd = None : legacy (foreground 検証なし、 warning 表示)
    # target_hwnd = int  : SendInput 直前に GetForegroundWindow() == target_hwnd 検証、
    #                      不一致なら送信 abort。 誤 window 流入事故を構造的に防ぐ。
    # foreground_retry_ms: 一致するまで N ms リトライ (200-500 推奨)。
    target_hwnd: int | None = None
    foreground_retry_ms: int = 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Automator core (MCP 層と 分離)
# ---------------------------------------------------------------------------

class Automator:
    """PC 自動化 core logic。 MCP に依存しない (単体 test 可能)。"""

    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        allowed_actions: frozenset[str] | None = None,
        auto_approve: bool = False,
    ):
        self.data_dir = data_dir
        self.allowed_actions = allowed_actions or ACTION_KINDS
        self.auto_approve = auto_approve
        self.pending: dict[str, Action] = {}
        self.executed: list[Action] = []
        self._id_counter = 0
        self._load_log()

    # ── lifecycle ─────────────────────────────────────────

    def propose(
        self, kind: str, label: str, command: str, source_module: str = "mcp",
        target_hwnd: int | None = None, foreground_retry_ms: int = 0,
    ) -> Action:
        if kind not in ACTION_KINDS:
            raise ValueError(f"未知の action kind: {kind}")
        self._id_counter += 1
        action = Action(
            id=f"ram-{self._id_counter}-{_now_ms()}",
            kind=kind,
            label=label,
            command=command,
            approved=self.auto_approve,
            logic_basis=f"{kind} from {source_module}",
            source_module=source_module,
            target_hwnd=target_hwnd,
            foreground_retry_ms=foreground_retry_ms,
        )
        self.pending[action.id] = action
        return action

    def approve(self, action_id: str) -> bool:
        action = self.pending.get(action_id)
        if action is None:
            return False
        if action.kind not in self.allowed_actions:
            return False
        action.approved = True
        return True

    def cancel(self, action_id: str) -> bool:
        return self.pending.pop(action_id, None) is not None

    def execute(self, action_id: str) -> dict[str, Any]:
        action = self.pending.get(action_id)
        if action is None:
            return {"success": False, "result": "action not found", "dfumt": DFUMT_FALSE}
        # ★ 最終関門 で allowlist を 独立 check (chat-Claude 2026-08-15 critique 対応)
        # auto_approve=True で propose が approve() を bypass するため、
        # approve() 側だけ の check では 迂回路 が 残る。 execute() 単独 で 判断できる状態
        # にしておく。 これで 「今後 approved を 立てる 経路が 増えても 同じ穴が 開かない」。
        if action.kind not in self.allowed_actions:
            return {
                "success": False,
                "result": f"kind not allowed: {action.kind}",
                "dfumt": DFUMT_FALSE,
            }
        if not action.approved:
            return {"success": False, "result": "not approved", "dfumt": DFUMT_NEITHER}

        started = time.perf_counter()
        try:
            result_text = self._dispatch(action)
            action.result = result_text
            action.executed_at = time.time()
            duration_ms = (time.perf_counter() - started) * 1000.0
            self.pending.pop(action_id, None)
            self.executed.append(action)
            self._save_log()
            return {
                "success": True,
                "action_id": action_id,
                "result": result_text,
                "duration_ms": round(duration_ms, 3),
                "dfumt": DFUMT_TRUE,
            }
        except Exception as e:
            action.result = f"error: {e}"
            duration_ms = (time.perf_counter() - started) * 1000.0
            return {
                "success": False,
                "action_id": action_id,
                "result": str(e),
                "duration_ms": round(duration_ms, 3),
                "dfumt": DFUMT_FALSE,
            }

    # ── dispatch (12 action kinds、 一部 MVP stub) ─────────

    def _dispatch(self, action: Action) -> str:
        k = action.kind
        cmd = action.command

        if k == "shell_command":
            # encoding は locale preference を 第一候補 (PowerShell 7 / 英語版 Windows で
            # UTF-8 返される case に 対応)、 cp932 は 従来 fallback。 chat-Claude 2026-08-15
            # critique 対応。
            r = subprocess.run(
                cmd, shell=True, capture_output=True, timeout=30,
            )
            enc = locale.getpreferredencoding(False) or "utf-8"
            out = r.stdout.decode(enc, errors="replace") if r.stdout else ""
            err = r.stderr.decode(enc, errors="replace") if r.stderr else ""
            body = out + (f"\n[stderr] {err}" if err else "")
            return f"exit={r.returncode}\n{body[:2000]}"

        if k == "file_read":
            p = Path(cmd)
            if not p.exists():
                return f"読み込み失敗: {cmd} が 存在しない"
            text = p.read_text(encoding="utf-8", errors="replace")
            return f"読み込み完了 ({len(text)}文字):\n{text[:2000]}"

        if k == "file_write":
            # command 形式: "path::content" (単純 protocol)
            if "::" not in cmd:
                return "file_write は 'path::content' 形式で 指定してください"
            path_str, content = cmd.split("::", 1)
            p = Path(path_str)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"書き込み完了: {path_str} ({len(content)} 文字)"

        if k == "type":
            # Phase 1.5 rei-sendinput.ps1 経由 (IME bypass)
            # a3 (2026-08-18) — foreground safety verify を pass-through
            return self._type_via_ps1(
                cmd,
                target_hwnd=action.target_hwnd,
                foreground_retry_ms=action.foreground_retry_ms,
            )

        if k == "find_element":
            # Phase 2 impl 順序 #2 (chat-Claude 設計書 §2-3、 search 改名)。
            # 副作用なし の read-only action、 click / type SetValue の共通基盤。
            return self._find_element_impl(cmd)

        if k == "wait":
            try:
                sec = float(cmd)
            except ValueError:
                return f"wait: {cmd} を 秒数として parse 失敗"
            time.sleep(sec)
            return f"待機完了 {sec} 秒"

        if k == "note_export":
            # command 形式: "path::title" — 簡易 note markdown 生成
            path_str, title = cmd.split("::", 1) if "::" in cmd else (cmd, "note")
            p = Path(path_str)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {title}\n\n{action.logic_basis}\n\n生成日時: {_now_iso()}\n", encoding="utf-8")
            return f"note export 完了: {path_str}"

        if k == "report":
            path_str, title = cmd.split("::", 1) if "::" in cmd else (cmd, "report")
            p = Path(path_str)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# レポート: {title}\n\n論理根拠: {action.logic_basis}\n生成日時: {_now_iso()}\n", encoding="utf-8")
            return f"report 生成完了: {path_str}"

        if k == "proof_run":
            return f"形式証明タスク登録: {cmd} (実行は Rei stack 側 defer)"

        # 以下 MVP stub (Phase 2 accessibility API 化で 本実装)
        if k == "screenshot":
            return f"stub: screenshot ({cmd}) — Phase 2 accessibility API で 本実装予定"
        if k == "open":
            # 単純に os.startfile で 開くだけ (Windows only)
            try:
                os.startfile(cmd)  # type: ignore[attr-defined]
                return f"open 完了: {cmd}"
            except Exception as e:
                return f"open 失敗: {e}"
        if k == "search":
            return f"stub: search ({cmd}) — 未実装 (search backend 選定 defer)"
        if k == "click":
            return f"stub: click ({cmd}) — Phase 2 UIA ClickablePoint pattern で 本実装予定"
        if k == "excel_aggregate":
            return f"stub: excel_aggregate ({cmd}) — openpyxl or Excel COM 連携 defer"

        return f"未知のアクション: {k} {cmd}"

    # ── find_element (Phase 2、 pywinauto uia backend) ───────
    #
    # command 形式:
    #   "title:保存"                                   → 名前で 探す (uia)
    #   "auto_id:btnSave"                              → AutomationId (最も安定)
    #   "control_type:Button"                          → control type で 探す
    #   "title:保存;backend=win32"                     → win32 明示指定 (fallback)
    #   "title:保存;max_depth=3;max_results=10"        → 上限指定
    #
    # design 方針 (docs/phase2-backend-design.md §1-1 / §2-3):
    #   - uia を 既定、 win32 は fallback (自動選択せず、 command で 明示指定)
    #   - ツリー全体を dump しない (max_depth / max_results で 明示的に上限)
    #   - 副作用なし の read-only action
    #   - pywinauto 未 install / 非 Windows でも crash せず graceful JSON error
    #
    # 戻り値は JSON string (result field に 埋め込み):
    #   {"found_count": int, "returned_count": int, "truncated": bool,
    #    "backend": "uia"|"win32", "selector": str, "results": [{name, auto_id,
    #    control_type, class_name, rect: {left, top, right, bottom}}, ...]}
    #
    # error 時 (pywinauto 未 install / import 失敗 / 実行時例外) も 同 shape で
    # {"error": str, "backend": str, "selector": str} を 返す (execute() は success=True)。

    def _find_element_impl(self, spec: str) -> str:
        # parse "selector_type:value[;key=value;...]"
        parts = [p.strip() for p in spec.split(";") if p.strip()]
        if not parts:
            return json.dumps({"error": "empty spec"}, ensure_ascii=False)
        selector = parts[0]
        opts: dict[str, str] = {}
        for kv in parts[1:]:
            if "=" in kv:
                key, val = kv.split("=", 1)
                opts[key.strip()] = val.strip()

        backend = opts.get("backend", "uia")
        try:
            max_results = max(1, min(int(opts.get("max_results", "25")), 200))
        except ValueError:
            max_results = 25
        try:
            max_depth = max(1, min(int(opts.get("max_depth", "4")), 10))
        except ValueError:
            max_depth = 4

        if ":" not in selector:
            return json.dumps(
                {
                    "error": "selector must be 'title:...' or 'auto_id:...' or 'control_type:...'",
                    "selector": selector,
                    "backend": backend,
                },
                ensure_ascii=False,
            )
        sel_kind, sel_value = selector.split(":", 1)
        sel_kind = sel_kind.strip()
        sel_value = sel_value.strip()

        if sel_kind not in {"title", "auto_id", "control_type"}:
            return json.dumps(
                {
                    "error": f"unknown selector kind: {sel_kind} (expected: title / auto_id / control_type)",
                    "selector": selector,
                    "backend": backend,
                },
                ensure_ascii=False,
            )
        if backend not in {"uia", "win32"}:
            return json.dumps(
                {
                    "error": f"unknown backend: {backend} (expected: uia / win32)",
                    "selector": selector,
                    "backend": backend,
                },
                ensure_ascii=False,
            )

        try:
            from pywinauto.findwindows import find_elements  # type: ignore[import-not-found]
        except ImportError:
            return json.dumps(
                {
                    "error": "pywinauto not installed. Run: pip install \"pywinauto>=0.6.9\"",
                    "hint": "Windows-only. See docs/phase2-backend-design.md §1",
                    "selector": selector,
                    "backend": backend,
                },
                ensure_ascii=False,
            )
        except OSError as e:
            # 非 Windows で pywinauto の win32 backend import が dll load 失敗する case
            return json.dumps(
                {
                    "error": f"pywinauto backend not available on this OS: {e}",
                    "selector": selector,
                    "backend": backend,
                },
                ensure_ascii=False,
            )

        kwargs: dict[str, Any] = {
            "backend": backend,
            "top_level_only": False,
            "depth": max_depth,
        }
        if sel_kind == "title":
            kwargs["title"] = sel_value
        elif sel_kind == "auto_id":
            kwargs["auto_id"] = sel_value
        elif sel_kind == "control_type":
            kwargs["control_type"] = sel_value

        try:
            found = find_elements(**kwargs)
        except Exception as e:  # pywinauto 内部の 多彩な例外を まとめて graceful 化
            return json.dumps(
                {
                    "error": f"find_elements failed: {type(e).__name__}: {e}",
                    "selector": selector,
                    "backend": backend,
                },
                ensure_ascii=False,
            )

        results: list[dict[str, Any]] = []
        for elem in found[:max_results]:
            try:
                rect = elem.rectangle
                results.append({
                    "name": (getattr(elem, "name", "") or "")[:200],
                    "auto_id": (getattr(elem, "auto_id", "") or "")[:200],
                    "control_type": (getattr(elem, "control_type", "") or "")[:100],
                    "class_name": (getattr(elem, "class_name", "") or "")[:100],
                    "rect": {
                        "left": int(getattr(rect, "left", 0)),
                        "top": int(getattr(rect, "top", 0)),
                        "right": int(getattr(rect, "right", 0)),
                        "bottom": int(getattr(rect, "bottom", 0)),
                    },
                })
            except Exception:
                # 個別 element の serialize 失敗は skip、 他の結果は返す
                continue

        return json.dumps(
            {
                "found_count": len(found),
                "returned_count": len(results),
                "truncated": len(found) > max_results,
                "backend": backend,
                "selector": selector,
                "results": results,
            },
            ensure_ascii=False,
        )

    # ── type via PS1 (Phase 1.5 統合) ─────────────────────

    def _type_via_ps1(
        self, text: str, target_hwnd: int | None = None,
        foreground_retry_ms: int = 0,
    ) -> str:
        if not PS1_PATH.exists():
            return (
                f"type failed: rei-sendinput.ps1 not found at {PS1_PATH}. "
                "Phase 1.5 script を bundle してから 再試行してください。"
            )
        ps_exe = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not ps_exe:
            return "type failed: powershell.exe / pwsh どちらも PATH に見つからない"
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        args = [
            ps_exe, "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(PS1_PATH),
            "-Base64", b64,
        ]
        if target_hwnd is not None:
            args += ["-ExpectedHwnd", str(int(target_hwnd))]
            if foreground_retry_ms > 0:
                args += ["-ForegroundRetryMs", str(int(foreground_retry_ms))]
        try:
            r = subprocess.run(args, capture_output=True, timeout=30)
            out = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
            err = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
            # a3 (2026-08-18): exit 3 は foreground mismatch (誤 window 流入防止 abort)。
            if r.returncode == 3 or "FOREGROUND_MISMATCH" in err or "FOREGROUND_MISMATCH" in out:
                return f"type aborted [foreground_mismatch]: {err.strip() or out.strip()}"
            if r.returncode != 0 or "OK sent=" not in out:
                return f"type failed: exit={r.returncode}\nstdout={out}\nstderr={err}"
            prefix = "type 完了"
            if target_hwnd is None:
                prefix += " [WARN: target_hwnd 未指定 = foreground verify skip、 誤 window 流入リスク]"
            return f"{prefix}: {out.strip()}"
        except subprocess.TimeoutExpired:
            return "type failed: PS1 実行 timeout (30 sec)"

    # ── persistence (STEP 1336 pattern) ───────────────────

    def _load_log(self) -> None:
        if not LOG_PATH.exists():
            return
        try:
            raw = LOG_PATH.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                self.executed = [Action(**item) for item in parsed]
                # 過去 id counter を restore
                past_ids = [
                    int(a.id.split("-")[1]) for a in self.executed
                    if a.id.startswith("ram-") and len(a.id.split("-")) >= 3
                ]
                if past_ids:
                    self._id_counter = max(past_ids)
        except (json.JSONDecodeError, TypeError, KeyError):
            # 破損 log は 無視、 空 executed で 継続
            pass

    def _save_log(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            LOG_PATH.write_text(
                json.dumps([asdict(a) for a in self.executed], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # persistence 失敗は execution を kill しない
            pass

    # ── status ─────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        return {
            "pending_count": len(self.pending),
            "executed_count": len(self.executed),
            "last_action": asdict(self.executed[-1]) if self.executed else None,
            "data_dir": str(self.data_dir),
            "log_path": str(LOG_PATH),
            "ps1_bundled": PS1_PATH.exists(),
        }


# ---------------------------------------------------------------------------
# global Automator instance
# ---------------------------------------------------------------------------

REI = Automator()


# ---------------------------------------------------------------------------
# MCP 層
# ---------------------------------------------------------------------------

def _register_mcp():
    """MCP server 起動 (import は runtime で 行う、 selftest は MCP 無依存)"""
    from mcp.server import MCPServer

    server = MCPServer(
        name="rei-automator",
        version="0.2.0a3",
        instructions=(
            "PC 自動化 (Windows) を propose → approve → execute の 3 段で 実行。 "
            "副作用の暴発を防ぐため、 execute は 承認済み action のみ 動作。 "
            "type action は IME bypass (SendInput + KEYEVENTF_UNICODE) で 日本語入力対応。 "
            "実行履歴は 自動 永続化 (~/.rei-automator-mcp/execution-log.json)。 "
            "13 action kinds: shell_command, file_read/write, screenshot, open, search, "
            "click, type, wait, note_export, excel_aggregate, report, proof_run, find_element。 "
            "Phase 1 MVP 実装済: shell/file/type/wait/note/report/open。 "
            "Phase 2 開始 (2026-08-15): find_element (pywinauto uia backend) 実装、 "
            "click / type SetValue 経路 / screenshot / excel は 順次 追加予定。"
        ),
    )

    @server.tool()
    def propose_action(kind: str, label: str, command: str, source_module: str = "mcp") -> dict[str, Any]:
        """PC 自動化 action を 提案する。 承認 (approve_action) が 出るまで 実行されない。

        Args:
            kind: action の 種類。 shell_command / file_read / file_write / screenshot /
                  open / search / click / type / wait / note_export / excel_aggregate /
                  report / proof_run / find_element の いずれか。
            label: action の 短い label (人間可読)。
            command: action の 引数。 kind に よって 意味が違う (shell なら command line、
                     file_read なら path、 file_write なら "path::content"、 type なら
                     入力する 文字列、 wait なら 秒数 (string)、 open なら 開く対象 path、
                     find_element なら "title:xxx" / "auto_id:xxx" / "control_type:xxx"
                     形式 (オプションで ";backend=win32;max_results=10" 併記可))。
            source_module: 提案元 module 名。 log 用。 default "mcp"。
        """
        try:
            a = REI.propose(kind, label, command, source_module)
            return {
                "action_id": a.id,
                "kind": a.kind,
                "label": a.label,
                "approved": a.approved,
                "dfumt": DFUMT_FLOWING,
            }
        except ValueError as e:
            return {"error": str(e), "dfumt": DFUMT_FALSE}

    @server.tool()
    def approve_action(action_id: str) -> dict[str, Any]:
        """提案済み action を 承認する。 承認後に execute_action で 実行可能になる。"""
        ok = REI.approve(action_id)
        return {
            "action_id": action_id,
            "approved": ok,
            "dfumt": DFUMT_TRUE if ok else DFUMT_FALSE,
            "reason": "approved" if ok else "action not found or kind not allowed",
        }

    @server.tool()
    def execute_action(action_id: str) -> dict[str, Any]:
        """承認済み action を 実行する。 結果と duration を 返す。 execution-log.json に 永続記録。"""
        return REI.execute(action_id)

    @server.tool()
    def cancel_action(action_id: str) -> dict[str, Any]:
        """提案済み action を 取り消す (未実行のもののみ)。"""
        ok = REI.cancel(action_id)
        return {
            "action_id": action_id,
            "cancelled": ok,
            "dfumt": DFUMT_NEITHER if ok else DFUMT_FALSE,
        }

    @server.tool()
    def list_pending() -> dict[str, Any]:
        """未実行の 提案済み action 一覧を 返す。"""
        return {"pending": [asdict(a) for a in REI.pending.values()]}

    @server.tool()
    def list_executed(limit: int = 20) -> dict[str, Any]:
        """実行済み action 一覧を 新しい順に 最大 limit 件 返す。"""
        recent = list(reversed(REI.executed[-limit:]))
        return {"executed": [asdict(a) for a in recent]}

    @server.tool()
    def get_status() -> dict[str, Any]:
        """Automator の 現状 (pending 数、 executed 数、 last action、 data_dir、 ps1 bundle 有無)。"""
        return REI.get_status()

    return server


# ---------------------------------------------------------------------------
# selftest (MCP 無依存、 core lifecycle + persistence + PS1 存在)
# ---------------------------------------------------------------------------

def _selftest() -> int:
    print("=== rei-automator-mcp selftest ===\n")
    passed = 0
    failed = 0

    def ok(cond: bool, msg: str) -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  OK: {msg}")
        else:
            failed += 1
            print(f"  FAIL: {msg}")

    # test 用 isolated data dir
    test_dir = Path.home() / ".rei-automator-mcp-selftest"
    if test_dir.exists():
        shutil.rmtree(test_dir)

    global REI
    REI = Automator(data_dir=test_dir)
    # test 用 LOG_PATH override
    global LOG_PATH
    LOG_PATH = test_dir / "execution-log.json"
    REI.data_dir = test_dir

    print("[1] propose/approve/execute lifecycle")
    a = REI.propose("file_read", "test read", str(Path(__file__).absolute()))
    ok(a.approved is False, "propose returns unapproved action")
    ok(REI.approve(a.id) is True, "approve returns True")
    r = REI.execute(a.id)
    ok(r["success"] is True, f"execute succeeds (got: {r.get('result', '')[:60]})")
    ok(r["dfumt"] == DFUMT_TRUE, "dfumt=TRUE on success")

    print("\n[2] persistence round-trip (STEP 1336 pattern)")
    ok(LOG_PATH.exists(), f"log file created at {LOG_PATH}")
    parsed = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    ok(len(parsed) == 1, f"log has 1 entry (got {len(parsed)})")
    ok(parsed[0]["id"] == a.id, "log entry id matches")

    print("\n[3] new instance loads prior log")
    b = Automator(data_dir=test_dir)
    ok(len(b.executed) == 1, f"instance B loaded prior log (got {len(b.executed)})")
    ok(b.executed[0].id == a.id, "loaded action id matches")

    print("\n[4] append + ordering")
    a2 = b.propose("file_read", "test read 2", str(Path(__file__).absolute()))
    b.approve(a2.id)
    r2 = b.execute(a2.id)
    ok(r2["success"] is True, "second execute succeeds")
    parsed2 = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    ok(len(parsed2) == 2, f"log grew to 2 entries (got {len(parsed2)})")
    ok(parsed2[0]["id"] == a.id and parsed2[1]["id"] == a2.id, "ordering preserved")

    print("\n[5] action kind allowlist (approve path)")
    c = Automator(data_dir=test_dir, allowed_actions=frozenset({"file_read"}))
    aw = c.propose("shell_command", "should reject", "echo hi")
    ok(c.approve(aw.id) is False, "approve rejects disallowed kind (shell_command)")

    print("\n[5b] allowlist は auto_approve を 貫通しない (chat-Claude 2026-08-15 regression)")
    d = Automator(
        data_dir=test_dir,
        allowed_actions=frozenset({"file_read"}),
        auto_approve=True,
    )
    aw2 = d.propose("shell_command", "should still be blocked", "echo PWNED")
    ok(aw2.approved is True, "[5b-1] auto_approve により propose の 時点で approved=True (design)")
    r_blocked = d.execute(aw2.id)
    ok(r_blocked["success"] is False, "[5b-2] execute が allowlist で reject (最終関門)")
    ok(
        "kind not allowed" in r_blocked["result"],
        "[5b-3] error message に kind not allowed 明示",
    )
    ok(r_blocked["dfumt"] == DFUMT_FALSE, "[5b-4] dfumt=FALSE on kind rejection")

    print("\n[6] PS1 bundle presence (rei-sendinput.ps1)")
    ok(PS1_PATH.exists(), f"PS1 bundled at {PS1_PATH}")
    if PS1_PATH.exists():
        head = PS1_PATH.read_bytes()[:3]
        ok(head == b"\xef\xbb\xbf", "PS1 has UTF-8 BOM (Windows PowerShell 5.1 compat)")

    print("\n[7] unknown kind rejected at propose")
    try:
        REI.propose("nonexistent_kind", "should fail", "x")
        ok(False, "unknown kind should raise ValueError")
    except ValueError:
        ok(True, "unknown kind raises ValueError")

    print("\n[8] get_status shape")
    st = b.get_status()
    ok("executed_count" in st and st["executed_count"] == 2, "status executed_count=2")
    ok("ps1_bundled" in st, "status includes ps1_bundled")

    print("\n[9] find_element (Phase 2 impl step #2, graceful degradation)")
    ok("find_element" in ACTION_KINDS, "[9-1] find_element registered in ACTION_KINDS")

    # 動作確認: pywinauto 未 install / 非 Windows でも crash せず、 JSON dict を 返すこと
    fe = b.propose("find_element", "test find", "title:__rei_nonexistent_window_probe__")
    ok(fe.approved is False, "[9-2] propose returns unapproved")
    ok(b.approve(fe.id) is True, "[9-3] approve returns True")
    r_fe = b.execute(fe.id)
    got = r_fe.get('result', '')[:80]
    ok(r_fe["success"] is True, f"[9-4] execute success=True (graceful), got: {got}")
    try:
        parsed_fe = json.loads(r_fe["result"])
        ok(isinstance(parsed_fe, dict), "[9-5] find_element result is JSON dict")
        # error 経路 (pywinauto 未 install) と 成功経路 (0 件 hit) 両方 accept
        has_error = "error" in parsed_fe
        has_results = "results" in parsed_fe and "found_count" in parsed_fe
        ok(has_error or has_results, f"[9-6] result has 'error' or 'results' field (got keys: {list(parsed_fe.keys())})")
    except json.JSONDecodeError:
        ok(False, f"[9-7] find_element result is not JSON: {r_fe.get('result', '')[:120]}")

    # spec parse error 検証 (pywinauto 有無に関わらず 独立に動く)
    fe2 = b.propose("find_element", "bad spec", "malformed_no_colon")
    b.approve(fe2.id)
    r_fe2 = b.execute(fe2.id)
    try:
        parsed_fe2 = json.loads(r_fe2["result"])
        ok("error" in parsed_fe2, "[9-8] malformed spec returns error field")
    except json.JSONDecodeError:
        ok(False, "[9-9] malformed spec result is not JSON")

    # unknown selector kind
    fe3 = b.propose("find_element", "unknown selector", "xpath://button[1]")
    b.approve(fe3.id)
    r_fe3 = b.execute(fe3.id)
    try:
        parsed_fe3 = json.loads(r_fe3["result"])
        ok("error" in parsed_fe3 and "unknown selector kind" in parsed_fe3.get("error", ""),
           "[9-10] unknown selector kind returns descriptive error")
    except json.JSONDecodeError:
        ok(False, "[9-11] unknown selector result is not JSON")

    print("\n[10] type action IME バイパス smoke test (a3 hardening + Phase 1.5 primitives verify)")
    # 実 PS1 実行は しない (Windows GUI 対象、 別 script scripts/manual-ime-test.py で cover)。
    # ここでは PS1 内容 + Python 側 経路 の 静的 verify + Japanese Unicode lifecycle のみ。

    ok(PS1_PATH.exists(), "[10-1] rei-sendinput.ps1 bundled (IME バイパス prerequisite)")

    if PS1_PATH.exists():
        ps1_bytes = PS1_PATH.read_bytes()
        ps1_text = (
            ps1_bytes[3:].decode("utf-8", errors="replace")
            if ps1_bytes[:3] == b"\xef\xbb\xbf"
            else ps1_bytes.decode("utf-8", errors="replace")
        )
        ok(
            "KEYEVENTF_UNICODE" in ps1_text,
            "[10-2] PS1 uses KEYEVENTF_UNICODE flag (IME バイパス primitive)",
        )
        ok(
            "SendInput" in ps1_text,
            "[10-3] PS1 uses Win32 SendInput API (not SendKeys / IME-dependent)",
        )
        ok(
            "VK_RETURN" in ps1_text and "VK_TAB" in ps1_text,
            "[10-4] PS1 handles control chars (\\n → VK_RETURN、 \\t → VK_TAB)",
        )
        ok(
            "ExpectedHwnd" in ps1_text and "FOREGROUND_MISMATCH" in ps1_text,
            "[10-5] PS1 has a3 foreground guard (ExpectedHwnd + FOREGROUND_MISMATCH exit 3)",
        )

    # Python 側 _type_via_ps1 の 経路 verify (source inspection)
    src_type = inspect.getsource(Automator._type_via_ps1)
    ok(
        "-Base64" in src_type,
        "[10-6] _type_via_ps1 invokes PS1 with -Base64 (UTF-8 safe transport、 -Text 未使用)",
    )
    ok(
        "-ExpectedHwnd" in src_type and "target_hwnd" in src_type,
        "[10-7] _type_via_ps1 passes -ExpectedHwnd through to PS1 (a3 integration)",
    )
    ok(
        "foreground_mismatch" in src_type,
        "[10-8] _type_via_ps1 detects PS1 exit 3 / FOREGROUND_MISMATCH (誤 window 流入 abort)",
    )

    # propose/approve/cancel lifecycle with Japanese Unicode (実 PS1 execution なし)
    japanese_text = "テスト日本語 IME バイパス"
    t = b.propose("type", "IME bypass dry (JP)", japanese_text)
    ok(
        t.kind == "type" and t.command == japanese_text,
        "[10-9] type action accepts Japanese Unicode command string (BMP 全 code point)",
    )
    b.approve(t.id)
    b.cancel(t.id)
    ok(
        t.id not in b.pending,
        "[10-10] type action lifecycle propose→approve→cancel (実 PS1 実行は 別 script)",
    )

    # cleanup
    shutil.rmtree(test_dir, ignore_errors=True)

    print(f"\n結果: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    server = _register_mcp()
    server.run(transport="stdio")
