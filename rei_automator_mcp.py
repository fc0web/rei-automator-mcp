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

Version: 0.1.0 (2026-08-15) — 初版、 rei-aios STEP 1336 直後の 独立抽出

License: MIT (v0.x)。 v1.0+ で AGPL-3.0 + commercial dual への 切替可能性。
"""

from __future__ import annotations

import base64
import json
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

    def propose(self, kind: str, label: str, command: str, source_module: str = "mcp") -> Action:
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
            # Windows: cp932 encoding で decode (default subprocess は cp932)
            r = subprocess.run(
                cmd, shell=True, capture_output=True, timeout=30,
            )
            out = r.stdout.decode("cp932", errors="replace") if r.stdout else ""
            err = r.stderr.decode("cp932", errors="replace") if r.stderr else ""
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
            return self._type_via_ps1(cmd)

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

    # ── type via PS1 (Phase 1.5 統合) ─────────────────────

    def _type_via_ps1(self, text: str) -> str:
        if not PS1_PATH.exists():
            return (
                f"type failed: rei-sendinput.ps1 not found at {PS1_PATH}. "
                "Phase 1.5 script を bundle してから 再試行してください。"
            )
        ps_exe = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not ps_exe:
            return "type failed: powershell.exe / pwsh どちらも PATH に見つからない"
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        try:
            r = subprocess.run(
                [
                    ps_exe, "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass",
                    "-File", str(PS1_PATH),
                    "-Base64", b64,
                ],
                capture_output=True, timeout=30,
            )
            out = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
            err = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
            if r.returncode != 0 or "OK sent=" not in out:
                return f"type failed: exit={r.returncode}\nstdout={out}\nstderr={err}"
            return f"type 完了: {out.strip()}"
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
        version="0.1.0",
        instructions=(
            "PC 自動化 (Windows) を propose → approve → execute の 3 段で 実行。 "
            "副作用の暴発を防ぐため、 execute は 承認済み action のみ 動作。 "
            "type action は IME bypass (SendInput + KEYEVENTF_UNICODE) で 日本語入力対応。 "
            "実行履歴は 自動 永続化 (~/.rei-automator-mcp/execution-log.json)。 "
            "12 action kinds: shell_command, file_read/write, screenshot, open, search, "
            "click, type, wait, note_export, excel_aggregate, report, proof_run。 "
            "Phase 1 MVP: shell/file/type/wait/note/report は 実装済、 "
            "screenshot/click/search/excel は Phase 2 accessibility API で 本実装予定。"
        ),
    )

    @server.tool()
    def propose_action(kind: str, label: str, command: str, source_module: str = "mcp") -> dict[str, Any]:
        """PC 自動化 action を 提案する。 承認 (approve_action) が 出るまで 実行されない。

        Args:
            kind: action の 種類。 shell_command / file_read / file_write / screenshot /
                  open / search / click / type / wait / note_export / excel_aggregate /
                  report / proof_run の いずれか。
            label: action の 短い label (人間可読)。
            command: action の 引数。 kind に よって 意味が違う (shell なら command line、
                     file_read なら path、 file_write なら "path::content"、 type なら
                     入力する 文字列、 wait なら 秒数 (string)、 open なら 開く対象 path)。
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

    print("\n[5] action kind allowlist")
    c = Automator(data_dir=test_dir, allowed_actions=frozenset({"file_read"}))
    aw = c.propose("shell_command", "should reject", "echo hi")
    ok(c.approve(aw.id) is False, "approve rejects disallowed kind (shell_command)")

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

    # cleanup
    shutil.rmtree(test_dir, ignore_errors=True)

    print(f"\n結果: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    server = _register_mcp()
    server.run(transport="stdio")
